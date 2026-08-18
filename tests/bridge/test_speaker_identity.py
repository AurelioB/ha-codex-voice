from __future__ import annotations

import json
import stat
import wave
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.speaker_identity import (
    CHUNK_BYTES,
    SAMPLE_RATE,
    SpeakerIdentityError,
    SpeakerIdentityStore,
    build_profile,
    identify,
    load_profile_documents,
    load_profiles,
)


def _pcm(seconds: int, *, amplitude: int = 1_000) -> bytes:
    return b"".join(
        (amplitude if index % 2 else -amplitude).to_bytes(2, "little", signed=True)
        for index in range(SAMPLE_RATE * seconds)
    )


def _wav(path: Path, pcm: bytes) -> Path:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(SAMPLE_RATE)
        target.writeframes(pcm)
    return path


@dataclass
class FakeEmbedder:
    vector: list[float]
    model_sha256: str = "a" * 64
    observed: list[bytes] = field(default_factory=list)

    def embed(self, pcm: bytes) -> list[float]:
        self.observed.append(pcm)
        return list(self.vector)


def test_private_profile_uses_multiple_chunks_and_loads_exact_model(
    tmp_path: Path,
) -> None:
    recordings = [_wav(tmp_path / "owner.wav", _pcm(15))]
    profiles = tmp_path / "profiles"
    embedder = FakeEmbedder([3.0, 4.0])

    record = build_profile(
        "owner",
        recordings,
        profiles=profiles,
        embedder=embedder,
    )

    path = profiles / "owner.json"
    assert record["chunks"] == 5
    assert all(len(chunk) == CHUNK_BYTES for chunk in embedder.observed)
    assert record["centroid"] == pytest.approx([0.6, 0.8])
    assert stat.S_IMODE(profiles.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_profiles(profiles, model_sha256="a" * 64) == {
        "owner": pytest.approx([0.6, 0.8])
    }
    with pytest.raises(SpeakerIdentityError, match="invalid speaker profile"):
        load_profiles(profiles, model_sha256="b" * 64)


def test_identify_strips_silence_and_requires_score_and_margin() -> None:
    embedder = FakeEmbedder([1.0, 0.0])
    pcm = b"\0" * SAMPLE_RATE * 2 * 2 + _pcm(3)

    result = identify(
        pcm,
        embedder=embedder,
        profiles={"owner": [1.0, 0.0], "other": [0.0, 1.0]},
        match_threshold=0.55,
        margin_threshold=0.08,
    )

    assert result == {
        "status": "match",
        "speaker_id": "owner",
        "score": 1.0,
        "margin": 1.0,
    }
    assert len(embedder.observed) == 1
    assert 2 * SAMPLE_RATE * 2 <= len(embedder.observed[0]) <= 3 * SAMPLE_RATE * 2

    ambiguous = identify(
        _pcm(3),
        embedder=FakeEmbedder([1.0, 1.0]),
        profiles={"owner": [1.0, 0.0], "other": [0.99, 0.01]},
        match_threshold=0.55,
        margin_threshold=0.08,
    )
    assert ambiguous["status"] == "unknown"


def test_short_voiced_audio_is_unknown_without_embedding() -> None:
    embedder = FakeEmbedder([1.0, 0.0])

    result = identify(
        _pcm(1) + b"\0" * SAMPLE_RATE * 2 * 3,
        embedder=embedder,
        profiles={"owner": [1.0, 0.0]},
        match_threshold=0.55,
        margin_threshold=0.08,
    )

    assert result["status"] == "unknown"
    assert embedder.observed == []


def test_invalid_profile_dimension_fails_closed(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "owner.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "speaker_id": "owner",
                "model_sha256": "a" * 64,
                "centroid": [],
            }
        )
    )

    with pytest.raises(SpeakerIdentityError, match="invalid dimension"):
        load_profiles(profiles, model_sha256="a" * 64)


def test_consented_enrollment_builds_disabled_linked_profile_without_raw_audio(
    tmp_path: Path,
) -> None:
    embedder = FakeEmbedder([1.0, 0.0])
    store = SpeakerIdentityStore(
        tmp_path / "profiles",
        embedder=embedder,
        match_threshold=0.55,
        margin_threshold=0.08,
    )

    enrollment = store.start_enrollment(
        {
            "speaker_id": "owner",
            "display_name": "Aurelio",
            "ha_person_id": "person.aurelio",
            "ha_user_id": "ha-user-id",
            "consent": True,
        }
    )
    assert enrollment["sample_count"] == 0
    for index in range(5):
        sample = store.add_enrollment_sample("owner", _pcm(5, amplitude=1_000 + index))
        assert sample["accepted"] is True
        assert sample["sample_count"] == index + 1

    profile = store.complete_enrollment("owner")
    assert profile == {
        "speaker_id": "owner",
        "display_name": "Aurelio",
        "ha_person_id": "person.aurelio",
        "ha_user_id": "ha-user-id",
        "enabled": False,
        "chunks": 5,
        "created_at": profile["created_at"],
        "updated_at": profile["updated_at"],
    }
    assert store.status()["raw_audio_retained"] is False
    assert not list((tmp_path / "profiles" / ".enrollments").glob("*.json"))
    serialized = (tmp_path / "profiles" / "owner.json").read_text()
    assert "RIFF" not in serialized
    assert "centroid" in serialized

    # Disabled profiles are excluded from ordinary recognition but available
    # for explicit held-out testing until the administrator activates them.
    assert store.identify_audio(_pcm(5))["status"] == "unknown"
    held_out = store.identify_audio(_pcm(5), include_disabled=True)
    assert held_out["speaker_id"] == "owner"
    assert held_out["display_name"] == "Aurelio"
    store.update_profile("owner", {"enabled": True})
    assert store.identify_audio(_pcm(5))["speaker_id"] == "owner"


def test_enrollment_requires_consent_is_single_active_and_deduplicates(
    tmp_path: Path,
) -> None:
    store = SpeakerIdentityStore(
        tmp_path / "profiles",
        embedder=FakeEmbedder([1.0, 0.0]),
        match_threshold=0.55,
        margin_threshold=0.08,
    )
    payload = {
        "speaker_id": "owner",
        "display_name": "Owner",
        "ha_person_id": None,
        "ha_user_id": None,
        "consent": False,
    }
    with pytest.raises(SpeakerIdentityError, match="explicit consent"):
        store.start_enrollment(payload)
    payload["consent"] = True
    store.start_enrollment(payload)
    duplicate = store.add_enrollment_sample("owner", _pcm(5))
    assert duplicate["accepted"] is True
    duplicate = store.add_enrollment_sample("owner", _pcm(5))
    assert duplicate["accepted"] is False
    assert duplicate["reason"] == "duplicate"
    assert duplicate["sample_count"] == 1
    with pytest.raises(SpeakerIdentityError, match="active speaker enrollment"):
        store.start_enrollment(
            {
                **payload,
                "speaker_id": "other",
                "display_name": "Other",
            }
        )


def test_profile_schema_one_migrates_in_memory_and_settings_persist(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "owner.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "speaker_id": "owner",
                "model_sha256": "a" * 64,
                "centroid": [1.0, 0.0],
            }
        )
    )
    documents = load_profile_documents(profiles, model_sha256="a" * 64)
    assert documents["owner"]["display_name"] == "owner"
    assert documents["owner"]["enabled"] is True

    store = SpeakerIdentityStore(
        profiles,
        embedder=FakeEmbedder([1.0, 0.0]),
        match_threshold=0.55,
        margin_threshold=0.08,
    )
    assert store.update_settings(
        {"match_threshold": 0.7, "margin_threshold": 0.12}
    ) == {"match_threshold": 0.7, "margin_threshold": 0.12}
    reloaded = SpeakerIdentityStore(
        profiles,
        embedder=FakeEmbedder([1.0, 0.0]),
        match_threshold=0.1,
        margin_threshold=0.01,
    )
    assert reloaded.settings() == {
        "match_threshold": 0.7,
        "margin_threshold": 0.12,
    }
    assert [profile["speaker_id"] for profile in reloaded.list_profiles()] == ["owner"]
