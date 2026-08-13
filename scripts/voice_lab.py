#!/usr/bin/env python3
"""Manage a private, consented dataset for realtime voice experiments."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import stat
import sys
import wave
from array import array
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CHANNELS = 1
MAX_WAV_BYTES = 1_100_000
KINDS = frozenset(
    {"wake-positive", "wake-negative", "speaker-enrollment", "background"}
)
OUTCOMES = frozenset({"hit", "miss", "false-activation", "not-evaluated"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_DURATION_LIMITS = {
    "wake-positive": (250, 5_000),
    "wake-negative": (250, 5_000),
    "speaker-enrollment": (1_000, 30_000),
    "background": (1_000, 30_000),
}


class VoiceLabError(ValueError):
    """A voice-lab dataset or sample is invalid."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _marker_path(root: Path) -> Path:
    return root / "voice-lab.json"


def _samples_path(root: Path) -> Path:
    return root / "samples"


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with temporary.open("x", encoding="utf-8") as target:
            temporary.chmod(0o600)
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def init_lab(root: Path) -> dict[str, Any]:
    """Initialize or validate one private voice-lab directory."""
    _private_directory(root)
    _private_directory(_samples_path(root))
    marker = _marker_path(root)
    if marker.exists():
        return load_index(root)
    value: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "created_at": _utc_now(),
        "samples": {},
    }
    _atomic_json(marker, value)
    return value


def load_index(root: Path) -> dict[str, Any]:
    """Load and minimally validate an initialized voice-lab index."""
    marker = _marker_path(root)
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VoiceLabError("voice lab is not initialized") from exc
    except json.JSONDecodeError as exc:
        raise VoiceLabError("voice-lab index is not valid JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != SCHEMA_VERSION
        or not isinstance(value.get("samples"), dict)
    ):
        raise VoiceLabError("voice-lab index has an unsupported schema")
    return value


def _validated_text(
    value: str | None,
    *,
    name: str,
    required: bool = False,
    limit: int,
) -> str | None:
    if value is None:
        if required:
            raise VoiceLabError(f"{name} is required")
        return None
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > limit:
        raise VoiceLabError(f"{name} must be non-empty and at most {limit} characters")
    return normalized


def _validated_id(value: str | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise VoiceLabError("speaker ID is required")
        return None
    if _ID.fullmatch(value) is None:
        raise VoiceLabError("speaker ID must contain only letters, digits, _, -, or .")
    return value


def _read_pcm(path: Path) -> tuple[bytes, int]:
    try:
        if path.stat().st_size > MAX_WAV_BYTES:
            raise VoiceLabError("WAV file is too large")
        with wave.open(str(path), "rb") as source:
            if (
                source.getcomptype() != "NONE"
                or source.getnchannels() != CHANNELS
                or source.getsampwidth() != SAMPLE_WIDTH
                or source.getframerate() != SAMPLE_RATE
            ):
                raise VoiceLabError("audio must be mono PCM16 WAV at 16 kHz")
            frames = source.getnframes()
            pcm = source.readframes(frames)
    except (EOFError, wave.Error) as exc:
        raise VoiceLabError("audio is not a valid WAV file") from exc
    if len(pcm) != frames * SAMPLE_WIDTH:
        raise VoiceLabError("WAV audio is truncated")
    return pcm, frames


def _canonical_wav(pcm: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(CHANNELS)
        target.setsampwidth(SAMPLE_WIDTH)
        target.setframerate(SAMPLE_RATE)
        target.writeframes(pcm)
    return output.getvalue()


def _levels(pcm: bytes) -> tuple[int, int]:
    values = array("h")
    values.frombytes(pcm)
    if sys.byteorder != "little":
        values.byteswap()
    if not values:
        return 0, 0
    peak = max(abs(value) for value in values)
    rms = round(math.sqrt(sum(value * value for value in values) / len(values)))
    return peak, rms


def _validate_labels(
    *,
    kind: str,
    speaker_id: str | None,
    phrase: str | None,
    outcome: str,
) -> tuple[str | None, str | None]:
    if kind not in KINDS:
        raise VoiceLabError(f"kind must be one of: {', '.join(sorted(KINDS))}")
    if outcome not in OUTCOMES:
        raise VoiceLabError(
            f"detector outcome must be one of: {', '.join(sorted(OUTCOMES))}"
        )
    speaker = _validated_id(speaker_id, required=kind == "speaker-enrollment")
    normalized_phrase = _validated_text(
        phrase,
        name="wake phrase",
        required=kind == "wake-positive",
        limit=80,
    )
    if kind == "wake-positive" and outcome == "false-activation":
        raise VoiceLabError("a wake-positive sample cannot be a false activation")
    if kind == "wake-negative":
        if normalized_phrase is not None:
            raise VoiceLabError("a wake-negative sample must not have a wake phrase")
        if outcome not in {"false-activation", "not-evaluated"}:
            raise VoiceLabError(
                "a wake-negative outcome must be false-activation or not-evaluated"
            )
    if kind in {"speaker-enrollment", "background"} and outcome != "not-evaluated":
        raise VoiceLabError(f"{kind} samples must use outcome not-evaluated")
    if kind == "background" and speaker is not None:
        raise VoiceLabError("a background sample cannot have a speaker ID")
    return speaker, normalized_phrase


def add_sample(
    root: Path,
    audio: Path,
    *,
    kind: str,
    consent: bool,
    speaker_id: str | None = None,
    phrase: str | None = None,
    outcome: str = "not-evaluated",
    provenance: str = "manual-import",
) -> dict[str, Any]:
    """Validate and copy one immutable sample into the private dataset."""
    if not consent:
        raise VoiceLabError("explicit consent is required to store voice audio")
    index = load_index(root)
    speaker, normalized_phrase = _validate_labels(
        kind=kind,
        speaker_id=speaker_id,
        phrase=phrase,
        outcome=outcome,
    )
    normalized_provenance = _validated_text(
        provenance, name="provenance", required=True, limit=120
    )
    pcm, frames = _read_pcm(audio)
    duration_ms = round(frames * 1_000 / SAMPLE_RATE)
    minimum, maximum = _DURATION_LIMITS[kind]
    if not minimum <= duration_ms <= maximum:
        raise VoiceLabError(
            f"{kind} audio must be between {minimum} and {maximum} milliseconds"
        )
    sample_id = hashlib.sha256(pcm).hexdigest()
    samples: dict[str, Any] = index["samples"]
    if sample_id in samples:
        raise VoiceLabError(f"sample already exists: {sample_id}")
    peak, rms = _levels(pcm)
    record: dict[str, Any] = {
        "id": sample_id,
        "kind": kind,
        "speaker_id": speaker,
        "phrase": normalized_phrase,
        "detector_outcome": outcome,
        "provenance": normalized_provenance,
        "consented_at": _utc_now(),
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "sample_width": SAMPLE_WIDTH,
        "frames": frames,
        "duration_ms": duration_ms,
        "peak": peak,
        "rms": rms,
        "file": f"samples/{sample_id}.wav",
    }
    destination = root / record["file"]
    canonical = _canonical_wav(pcm)
    try:
        with destination.open("xb") as target:
            destination.chmod(0o600)
            target.write(canonical)
            target.flush()
            os.fsync(target.fileno())
        samples[sample_id] = record
        _atomic_json(_marker_path(root), index)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return record


def _verify_sample(root: Path, sample_id: str, record: dict[str, Any]) -> None:
    path = root / str(record["file"])
    if path.parent != _samples_path(root):
        raise VoiceLabError("sample path escapes the samples directory")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise VoiceLabError("sample permissions are not private")
    pcm, frames = _read_pcm(path)
    if hashlib.sha256(pcm).hexdigest() != sample_id:
        raise VoiceLabError("sample digest does not match its ID")
    if frames != record.get("frames"):
        raise VoiceLabError("sample frame count does not match its index")


def verify_lab(root: Path) -> dict[str, int]:
    """Verify every indexed sample's format, digest, and private permissions."""
    index = load_index(root)
    errors = 0
    checked = 0
    if stat.S_IMODE(root.stat().st_mode) != 0o700:
        errors += 1
    if stat.S_IMODE(_marker_path(root).stat().st_mode) != 0o600:
        errors += 1
    for sample_id, record in index["samples"].items():
        checked += 1
        try:
            _verify_sample(root, sample_id, record)
        except (KeyError, OSError, TypeError, VoiceLabError):
            errors += 1
    return {"checked": checked, "errors": errors}


def remove_sample(root: Path, sample_id: str) -> dict[str, Any]:
    """Remove one sample and its index record."""
    index = load_index(root)
    samples: dict[str, Any] = index["samples"]
    try:
        record = samples.pop(sample_id)
    except KeyError as exc:
        raise VoiceLabError("sample ID was not found") from exc
    path = root / str(record["file"])
    path.unlink(missing_ok=True)
    _atomic_json(_marker_path(root), index)
    return record


def list_samples(
    root: Path,
    *,
    kind: str | None = None,
    speaker_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return samples matching optional exact labels."""
    index = load_index(root)
    return [
        record
        for record in index["samples"].values()
        if (kind is None or record.get("kind") == kind)
        and (speaker_id is None or record.get("speaker_id") == speaker_id)
    ]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")

    add = commands.add_parser("add")
    add.add_argument("audio", type=Path)
    add.add_argument("--kind", choices=sorted(KINDS), required=True)
    add.add_argument("--speaker-id")
    add.add_argument("--phrase")
    add.add_argument("--outcome", choices=sorted(OUTCOMES), default="not-evaluated")
    add.add_argument("--provenance", default="manual-import")
    add.add_argument("--consent", action="store_true")

    listing = commands.add_parser("list")
    listing.add_argument("--kind", choices=sorted(KINDS))
    listing.add_argument("--speaker-id")

    commands.add_parser("verify")
    remove = commands.add_parser("remove")
    remove.add_argument("sample_id")
    return parser.parse_args()


def main() -> int:
    """Run the selected voice-lab operation."""
    args = _arguments()
    try:
        if args.command == "init":
            result: object = init_lab(args.root)
        elif args.command == "add":
            result = add_sample(
                args.root,
                args.audio,
                kind=args.kind,
                consent=args.consent,
                speaker_id=args.speaker_id,
                phrase=args.phrase,
                outcome=args.outcome,
                provenance=args.provenance,
            )
        elif args.command == "list":
            result = list_samples(args.root, kind=args.kind, speaker_id=args.speaker_id)
        elif args.command == "verify":
            result = verify_lab(args.root)
            if result["errors"]:
                print(json.dumps(result, indent=2, sort_keys=True))
                return 1
        else:
            result = remove_sample(args.root, args.sample_id)
    except (OSError, VoiceLabError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
