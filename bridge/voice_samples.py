"""Private opt-in storage for explicitly consented wake-word samples."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import BridgeError, ProtocolError

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
MIN_WAKE_SAMPLE_BYTES = SAMPLE_RATE * SAMPLE_WIDTH // 2
MAX_WAKE_SAMPLE_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * 4
MAX_WAKE_PHRASE_CHARS = 80
FALSE_WAKE_MAX_AGE_SECONDS = 10 * 60

MARK_FALSE_WAKE_TOOL_NAME = "mark_false_wake"
MARK_FALSE_WAKE_TOOL: dict[str, Any] = {
    "type": "function",
    "name": MARK_FALSE_WAKE_TOOL_NAME,
    "description": (
        "Mark this session's most recent wake as a false activation for the next "
        "local wake-word training run. Use only when the user explicitly says the "
        "speaker woke by mistake or nobody called it."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}


class VoiceSampleUnavailable(BridgeError):
    """The private sample inbox cannot complete the requested operation."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, mode=0o700)
        metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise VoiceSampleUnavailable("voice sample path must be a real directory")
    path.chmod(0o700)


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
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


def _write_wav(path: Path, pcm: bytes) -> None:
    with path.open("xb") as raw:
        path.chmod(0o600)
        with wave.open(raw, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(SAMPLE_WIDTH)
            target.setframerate(SAMPLE_RATE)
            target.writeframes(pcm)
        raw.flush()
        os.fsync(raw.fileno())


class VoiceSampleInbox:
    """Store bounded wake samples only when an owner explicitly enables it."""

    def __init__(self, root: str | None) -> None:
        self._root = Path(root) if root is not None else None
        self._lock = threading.Lock()
        self._stored = 0
        self._false_wakes = 0
        self._failures = 0
        if self._root is not None:
            _private_directory(self._root)
            _private_directory(self._root / "inbox")

    @property
    def enabled(self) -> bool:
        return self._root is not None

    @property
    def tools(self) -> tuple[dict[str, Any], ...]:
        return (MARK_FALSE_WAKE_TOOL,) if self.enabled else ()

    def owns(self, name: object) -> bool:
        return self.enabled and name == MARK_FALSE_WAKE_TOOL_NAME

    def health(self) -> dict[str, bool | int]:
        return {
            "enabled": self.enabled,
            "samples_stored": self._stored,
            "false_wakes_labeled": self._false_wakes,
            "failures": self._failures,
        }

    def store_wake(self, pcm: bytes, *, phrase: str) -> None:
        """Persist one immutable, initially-unreviewed wake capture."""
        if self._root is None:
            raise VoiceSampleUnavailable("voice sample collection is disabled")
        normalized_phrase = " ".join(phrase.split())
        if not normalized_phrase or len(normalized_phrase) > MAX_WAKE_PHRASE_CHARS:
            raise ProtocolError("wake phrase must be non-empty and bounded")
        if (
            len(pcm) % SAMPLE_WIDTH
            or not MIN_WAKE_SAMPLE_BYTES <= len(pcm) <= MAX_WAKE_SAMPLE_BYTES
        ):
            raise ProtocolError("wake sample must contain 0.5 to 4 seconds of PCM16")
        digest = hashlib.sha256(pcm).hexdigest()
        captured_at = _utc_now()
        stamp = time.time_ns()
        stem = f"wake-{stamp}-{digest[:16]}"
        directory = self._root / "inbox"
        wav_path = directory / f"{stem}.wav"
        metadata_path = directory / f"{stem}.json"
        record = {
            "schema_version": 1,
            "id": digest,
            "captured_at": captured_at,
            "kind": "unreviewed-wake",
            "detector_outcome": "hit",
            "phrase": normalized_phrase,
            "sample_rate": SAMPLE_RATE,
            "channels": 1,
            "sample_width": SAMPLE_WIDTH,
            "frames": len(pcm) // SAMPLE_WIDTH,
            "file": wav_path.name,
        }
        try:
            with self._lock:
                _write_wav(wav_path, pcm)
                try:
                    _atomic_json(metadata_path, record)
                except BaseException:
                    wav_path.unlink(missing_ok=True)
                    raise
                self._stored += 1
        except BaseException:
            self._failures += 1
            raise

    def mark_latest_false_wake(self) -> dict[str, object]:
        """Label only a recent unreviewed capture after explicit user intent."""
        if self._root is None:
            raise VoiceSampleUnavailable("voice sample collection is disabled")
        now_ns = time.time_ns()
        try:
            with self._lock:
                candidates = sorted((self._root / "inbox").glob("wake-*.json"))
                for metadata_path in reversed(candidates):
                    try:
                        record = json.loads(metadata_path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        continue
                    if (
                        not isinstance(record, dict)
                        or record.get("kind") != "unreviewed-wake"
                    ):
                        continue
                    parts = metadata_path.stem.split("-", 2)
                    try:
                        captured_ns = int(parts[1])
                    except (IndexError, ValueError):
                        continue
                    age = max(0.0, (now_ns - captured_ns) / 1_000_000_000)
                    if age > FALSE_WAKE_MAX_AGE_SECONDS:
                        break
                    record["kind"] = "wake-negative"
                    record["detector_outcome"] = "false-activation"
                    record["labeled_at"] = _utc_now()
                    _atomic_json(metadata_path, record)
                    self._false_wakes += 1
                    return {"status": "marked", "do_not_retry": True}
        except BaseException:
            self._failures += 1
            raise
        raise VoiceSampleUnavailable("no recent unreviewed wake sample exists")

    def close(self) -> None:
        """Compatibility boundary for future asynchronous storage backends."""
        return
