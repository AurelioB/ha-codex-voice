from __future__ import annotations

import stat
import wave
from pathlib import Path

import pytest

from scripts.voice_lab import (
    VoiceLabError,
    add_sample,
    export_wake_manifest,
    init_lab,
    list_samples,
    load_index,
    remove_sample,
    verify_lab,
)


def _wav(
    path: Path,
    *,
    frames: int = 16_000,
    sample_rate: int = 16_000,
    amplitude: int = 1_000,
) -> Path:
    pcm = b"".join(
        (amplitude if index % 2 else -amplitude).to_bytes(2, "little", signed=True)
        for index in range(frames)
    )
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(pcm)
    return path


def test_private_lab_import_list_verify_and_remove(tmp_path: Path) -> None:
    root = tmp_path / "private-voice-lab"
    audio = _wav(tmp_path / "wake.wav")

    init_lab(root)
    record = add_sample(
        root,
        audio,
        kind="wake-positive",
        consent=True,
        speaker_id="owner",
        phrase="Okay Nabu",
        outcome="miss",
        provenance="manual-test",
    )

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "voice-lab.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / record["file"]).stat().st_mode) == 0o600
    assert record["duration_ms"] == 1_000
    assert record["peak"] == 1_000
    assert record["rms"] == 1_000
    assert list_samples(root, kind="wake-positive") == [record]
    assert list_samples(root, speaker_id="someone-else") == []
    assert verify_lab(root) == {"checked": 1, "errors": 0}

    assert remove_sample(root, record["id"]) == record
    assert list_samples(root) == []
    assert not (root / record["file"]).exists()


def test_import_requires_consent_and_consistent_labels(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    audio = _wav(tmp_path / "sample.wav")
    init_lab(root)

    with pytest.raises(VoiceLabError, match="consent"):
        add_sample(root, audio, kind="wake-positive", consent=False, phrase="wake")
    with pytest.raises(VoiceLabError, match="wake phrase is required"):
        add_sample(root, audio, kind="wake-positive", consent=True)
    with pytest.raises(VoiceLabError, match="must not have a wake phrase"):
        add_sample(
            root,
            audio,
            kind="wake-negative",
            consent=True,
            phrase="wake",
            outcome="false-activation",
        )
    with pytest.raises(VoiceLabError, match="speaker ID is required"):
        add_sample(root, audio, kind="speaker-enrollment", consent=True)


def test_duplicate_pcm_is_rejected_across_source_files(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    first = _wav(tmp_path / "first.wav")
    second = _wav(tmp_path / "second.wav")
    init_lab(root)
    add_sample(
        root,
        first,
        kind="wake-positive",
        consent=True,
        phrase="okay nabu",
    )

    with pytest.raises(VoiceLabError, match="already exists"):
        add_sample(
            root,
            second,
            kind="speaker-enrollment",
            consent=True,
            speaker_id="owner",
        )


def test_invalid_audio_and_tampering_fail_verification(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    wrong_rate = _wav(tmp_path / "wrong.wav", sample_rate=24_000)
    audio = _wav(tmp_path / "valid.wav")
    init_lab(root)

    with pytest.raises(VoiceLabError, match="16 kHz"):
        add_sample(
            root,
            wrong_rate,
            kind="wake-positive",
            consent=True,
            phrase="okay nabu",
        )

    record = add_sample(
        root,
        audio,
        kind="speaker-enrollment",
        consent=True,
        speaker_id="owner",
    )
    (root / record["file"]).chmod(0o644)
    assert verify_lab(root) == {"checked": 1, "errors": 1}


def test_existing_index_is_idempotent_and_loaded(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    created = init_lab(root)

    assert init_lab(root) == created
    assert load_index(root) == created


def test_wake_export_is_private_deterministic_and_grouped_by_session(
    tmp_path: Path,
) -> None:
    root = tmp_path / "lab"
    init_lab(root)
    for index, (kind, session_id) in enumerate(
        [
            ("wake-positive", "positive-session"),
            ("wake-positive", "positive-session"),
            ("wake-negative", "negative-session"),
            ("background", "background-session"),
        ],
        start=1,
    ):
        add_sample(
            root,
            _wav(tmp_path / f"sample-{index}.wav", amplitude=1_000 + index),
            kind=kind,
            consent=True,
            phrase="Okay Nabu" if kind == "wake-positive" else None,
            outcome="hit" if kind == "wake-positive" else "not-evaluated",
            provenance=f"capture-{index}",
            session_id=session_id,
        )

    output = root / "artifacts" / "okay-nabu-training.json"
    first = export_wake_manifest(root, output, phrase="okay nabu")
    first_bytes = output.read_bytes()
    second = export_wake_manifest(root, output, phrase="okay nabu")

    assert first == second
    assert output.read_bytes() == first_bytes
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert first["counts"] == {"positive": 2, "negative": 2, "total": 4}
    positive_splits = {
        sample["split"]
        for sample in first["samples"]
        if sample["group"] == "positive-session"
    }
    assert len(positive_splits) == 1

    with pytest.raises(VoiceLabError, match="inside the private voice lab"):
        export_wake_manifest(
            root,
            tmp_path / "public.json",
            phrase="okay nabu",
        )
