from __future__ import annotations

import stat
import wave
from pathlib import Path

import pytest

from scripts.voice_lab import (
    VoiceLabError,
    add_sample,
    init_lab,
    list_samples,
    load_index,
    remove_sample,
    verify_lab,
)


def _wav(path: Path, *, frames: int = 16_000, sample_rate: int = 16_000) -> Path:
    pcm = b"".join(
        (1_000 if index % 2 else -1_000).to_bytes(2, "little", signed=True)
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
