#!/usr/bin/env python3
"""Build and serve private speaker embeddings from consented PCM16 WAV files."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib
import json
import math
import os
import re
import sys
import wave
from array import array
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlsplit

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CHUNK_SECONDS = 3
CHUNK_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * CHUNK_SECONDS
MIN_ENROLLMENT_CHUNKS = 5
MAX_IDENTIFY_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * 10
MIN_IDENTIFY_BYTES = SAMPLE_RATE * SAMPLE_WIDTH
MIN_VOICED_IDENTIFY_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * 2
MIN_CHUNK_RMS = 164
MAX_PROFILES = 32
MAX_EMBEDDING_DIMENSIONS = 2_048
PROFILE_SCHEMA_VERSION = 2
ENROLLMENT_SCHEMA_VERSION = 1
SETTINGS_SCHEMA_VERSION = 1
ENROLLMENTS_DIRECTORY = ".enrollments"
SETTINGS_FILENAME = ".settings.json"
MIN_ENROLLMENT_VECTORS = 5
MAX_DISPLAY_NAME_CHARS = 128
MAX_HA_ID_CHARS = 256
MAX_JSON_BODY_BYTES = 16 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class SpeakerIdentityError(ValueError):
    """Speaker enrollment, profile data, or inference input is invalid."""


class Embedder(Protocol):
    """Minimal embedding backend used by enrollment and inference."""

    @property
    def model_sha256(self) -> str: ...

    def embed(self, pcm: bytes) -> list[float] | None: ...


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validated_optional_text(
    value: object,
    *,
    field: str,
    limit: int,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SpeakerIdentityError(f"{field} must be text")
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > limit
        or any(not character.isprintable() for character in normalized)
    ):
        raise SpeakerIdentityError(f"{field} must be non-empty bounded text")
    return normalized


def _validated_speaker_id(value: object) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise SpeakerIdentityError("speaker ID contains unsupported characters")
    return value


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as target:
            temporary.chmod(0o600)
            json.dump(value, target, separators=(",", ":"), sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


class SherpaOnnxEmbedder:
    """Lazy host-side sherpa-onnx speaker embedding backend."""

    def __init__(self, model: Path, *, expected_sha256: str) -> None:
        """Load one exact model after verifying its complete digest."""
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise SpeakerIdentityError("model SHA-256 must be 64 lowercase hex digits")
        observed = _sha256_file(model)
        if not hmac.compare_digest(observed, expected_sha256):
            raise SpeakerIdentityError("speaker model digest does not match")
        try:
            np = importlib.import_module("numpy")
            sherpa_onnx = importlib.import_module("sherpa_onnx")
        except ImportError as exc:
            raise SpeakerIdentityError(
                "speaker identity requires numpy and sherpa-onnx"
            ) from exc
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(model),
            num_threads=2,
        )
        if not config.validate():
            raise SpeakerIdentityError("speaker embedding configuration is invalid")
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self._numpy = np
        self._model_sha256 = observed

    @property
    def model_sha256(self) -> str:
        return self._model_sha256

    def embed(self, pcm: bytes) -> list[float] | None:
        np = self._numpy
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        stream = self._extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio)
        stream.input_finished()
        if not self._extractor.is_ready(stream):
            return None
        return _normalize([float(value) for value in self._extractor.compute(stream)])


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_wav(path: Path) -> bytes:
    try:
        with wave.open(str(path), "rb") as source:
            if (
                source.getcomptype() != "NONE"
                or source.getnchannels() != 1
                or source.getsampwidth() != SAMPLE_WIDTH
                or source.getframerate() != SAMPLE_RATE
            ):
                raise SpeakerIdentityError("audio must be mono PCM16 WAV at 16 kHz")
            frames = source.getnframes()
            pcm = source.readframes(frames)
    except (EOFError, OSError, wave.Error) as exc:
        raise SpeakerIdentityError(f"cannot read enrollment WAV: {path}") from exc
    if len(pcm) != frames * SAMPLE_WIDTH:
        raise SpeakerIdentityError("enrollment WAV is truncated")
    return pcm


def _rms(pcm: bytes) -> int:
    values = array("h")
    values.frombytes(pcm)
    if sys.byteorder != "little":
        values.byteswap()
    if not values:
        return 0
    return round(math.sqrt(sum(value * value for value in values) / len(values)))


def _normalize(values: list[float]) -> list[float]:
    if not values or len(values) > MAX_EMBEDDING_DIMENSIONS:
        raise SpeakerIdentityError("speaker embedding has an invalid dimension")
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or norm <= 0:
        raise SpeakerIdentityError("speaker embedding is not finite")
    return [value / norm for value in values]


def _mean_normalized(vectors: list[list[float]]) -> list[float]:
    if not vectors or len({len(vector) for vector in vectors}) != 1:
        raise SpeakerIdentityError("speaker embeddings have inconsistent dimensions")
    return _normalize(
        [
            sum(vector[index] for vector in vectors) / len(vectors)
            for index in range(len(vectors[0]))
        ]
    )


def _usable_chunks(pcm: bytes) -> list[bytes]:
    chunks: list[bytes] = []
    for offset in range(0, len(pcm) - CHUNK_BYTES + 1, CHUNK_BYTES):
        chunk = pcm[offset : offset + CHUNK_BYTES]
        if _rms(chunk) >= MIN_CHUNK_RMS:
            chunks.append(chunk)
    return chunks


def _voiced_only(pcm: bytes) -> bytes:
    """Remove quiet 50 ms frames before computing a live identity embedding."""
    frame_bytes = SAMPLE_RATE * SAMPLE_WIDTH // 20
    frames = [
        pcm[offset : offset + frame_bytes]
        for offset in range(0, len(pcm) - frame_bytes + 1, frame_bytes)
    ]
    if not frames:
        return b""
    levels = [_rms(frame) for frame in frames]
    gate = max(round(max(levels) * 0.08), 4)
    return b"".join(
        frame for frame, level in zip(frames, levels, strict=True) if level > gate
    )


def build_profile(
    speaker_id: str,
    recordings: list[Path],
    *,
    profiles: Path,
    embedder: Embedder,
    display_name: str | None = None,
    ha_person_id: str | None = None,
    ha_user_id: str | None = None,
    enabled: bool = True,
) -> dict[str, object]:
    """Build one normalized centroid from several consented recordings."""
    speaker_id = _validated_speaker_id(speaker_id)
    display_name = _validated_optional_text(
        display_name,
        field="display name",
        limit=MAX_DISPLAY_NAME_CHARS,
    )
    ha_person_id = _validated_optional_text(
        ha_person_id,
        field="Home Assistant person ID",
        limit=MAX_HA_ID_CHARS,
    )
    ha_user_id = _validated_optional_text(
        ha_user_id,
        field="Home Assistant user ID",
        limit=MAX_HA_ID_CHARS,
    )
    vectors: list[list[float]] = []
    source_hashes: list[str] = []
    for recording in recordings:
        pcm = _read_wav(recording)
        source_hashes.append(hashlib.sha256(pcm).hexdigest())
        for chunk in _usable_chunks(pcm):
            vector = embedder.embed(chunk)
            if vector is not None:
                vectors.append(_normalize(vector))
    if len(vectors) < MIN_ENROLLMENT_CHUNKS:
        raise SpeakerIdentityError(
            f"only {len(vectors)} usable enrollment chunks; need at least "
            f"{MIN_ENROLLMENT_CHUNKS}"
        )
    profiles.mkdir(parents=True, exist_ok=True, mode=0o700)
    profiles.chmod(0o700)
    record: dict[str, object] = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "speaker_id": speaker_id,
        "model_sha256": embedder.model_sha256,
        "source_sha256": source_hashes,
        "chunks": len(vectors),
        "centroid": _mean_normalized(vectors),
        "display_name": display_name or speaker_id,
        "ha_person_id": ha_person_id,
        "ha_user_id": ha_user_id,
        "enabled": enabled,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    destination = profiles / f"{speaker_id}.json"
    _atomic_json(destination, record)
    return record


def load_profiles(profiles: Path, *, model_sha256: str) -> dict[str, list[float]]:
    """Load a bounded closed set matching the active model exactly."""
    documents = load_profile_documents(profiles, model_sha256=model_sha256)
    return {
        speaker_id: [float(item) for item in document["centroid"]]
        for speaker_id, document in documents.items()
        if document["enabled"] is True
    }


def load_profile_documents(
    profiles: Path,
    *,
    model_sha256: str,
    include_disabled: bool = True,
) -> dict[str, dict[str, object]]:
    """Load private profile records, migrating schema-one fields in memory."""
    loaded: dict[str, dict[str, object]] = {}
    try:
        paths = sorted(
            path for path in profiles.glob("*.json") if not path.name.startswith(".")
        )
    except OSError as exc:
        raise SpeakerIdentityError("cannot read speaker profiles") from exc
    if len(paths) > MAX_PROFILES:
        raise SpeakerIdentityError("too many speaker profiles")
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SpeakerIdentityError(f"invalid speaker profile: {path.name}") from exc
        schema_version = value.get("schema_version")
        if (
            not isinstance(value, dict)
            or schema_version not in {1, PROFILE_SCHEMA_VERSION}
            or value.get("model_sha256") != model_sha256
            or not isinstance(value.get("speaker_id"), str)
            or _ID.fullmatch(value["speaker_id"]) is None
            or not isinstance(value.get("centroid"), list)
            or not all(isinstance(item, (int, float)) for item in value["centroid"])
        ):
            raise SpeakerIdentityError(f"invalid speaker profile: {path.name}")
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise SpeakerIdentityError(f"invalid speaker profile: {path.name}")
        if not include_disabled and not enabled:
            continue
        speaker_id = value["speaker_id"]
        display_name = _validated_optional_text(
            value.get("display_name", speaker_id),
            field="display name",
            limit=MAX_DISPLAY_NAME_CHARS,
        )
        ha_person_id = _validated_optional_text(
            value.get("ha_person_id"),
            field="Home Assistant person ID",
            limit=MAX_HA_ID_CHARS,
        )
        ha_user_id = _validated_optional_text(
            value.get("ha_user_id"),
            field="Home Assistant user ID",
            limit=MAX_HA_ID_CHARS,
        )
        chunks = value.get("chunks", 0)
        if isinstance(chunks, bool) or not isinstance(chunks, int) or chunks < 0:
            raise SpeakerIdentityError(f"invalid speaker profile: {path.name}")
        loaded[speaker_id] = {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "speaker_id": speaker_id,
            "model_sha256": model_sha256,
            "source_sha256": value.get("source_sha256", []),
            "chunks": chunks,
            "centroid": _normalize([float(item) for item in value["centroid"]]),
            "display_name": display_name or speaker_id,
            "ha_person_id": ha_person_id,
            "ha_user_id": ha_user_id,
            "enabled": enabled,
            "created_at": value.get("created_at"),
            "updated_at": value.get("updated_at"),
        }
    return loaded


def identify(
    pcm: bytes,
    *,
    embedder: Embedder,
    profiles: dict[str, list[float]],
    match_threshold: float,
    margin_threshold: float,
) -> dict[str, object]:
    """Return a conservative closed-set match or unknown."""
    if (
        len(pcm) % SAMPLE_WIDTH
        or not MIN_IDENTIFY_BYTES <= len(pcm) <= MAX_IDENTIFY_BYTES
    ):
        raise SpeakerIdentityError("identity audio must be 1 to 10 seconds of PCM16")
    voiced = _voiced_only(pcm)
    if len(voiced) < MIN_VOICED_IDENTIFY_BYTES:
        return {"status": "unknown", "score": 0.0, "margin": 0.0}
    vector = embedder.embed(voiced)
    if vector is None or not profiles:
        return {"status": "unknown", "score": 0.0, "margin": 0.0}
    normalized = _normalize(vector)
    if any(len(centroid) != len(normalized) for centroid in profiles.values()):
        raise SpeakerIdentityError("profile embedding dimension does not match model")
    scored = sorted(
        (
            (sum(a * b for a, b in zip(normalized, centroid, strict=True)), name)
            for name, centroid in profiles.items()
        ),
        reverse=True,
    )
    best_score, best_name = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else -1.0
    margin = best_score - second_score
    if best_score < match_threshold or margin < margin_threshold:
        return {
            "status": "unknown",
            "score": round(best_score, 6),
            "margin": round(margin, 6),
        }
    return {
        "status": "match",
        "speaker_id": best_name,
        "score": round(best_score, 6),
        "margin": round(margin, 6),
    }


class SpeakerIdentityStore:
    """Persist profiles, consented enrollment embeddings, and live thresholds."""

    def __init__(
        self,
        profiles_path: Path,
        *,
        embedder: Embedder,
        match_threshold: float,
        margin_threshold: float,
    ) -> None:
        self.profiles_path = profiles_path
        self.embedder = embedder
        self.profiles_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profiles_path.chmod(0o700)
        self.enrollments_path = self.profiles_path / ENROLLMENTS_DIRECTORY
        self.enrollments_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.enrollments_path.chmod(0o700)
        self.settings_path = self.profiles_path / SETTINGS_FILENAME
        self.match_threshold = match_threshold
        self.margin_threshold = margin_threshold
        self._load_settings()

    def _load_settings(self) -> None:
        if not self.settings_path.exists():
            return
        try:
            value = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SpeakerIdentityError("cannot read speaker identity settings") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise SpeakerIdentityError("speaker identity settings are invalid")
        self._set_thresholds(
            value.get("match_threshold"),
            value.get("margin_threshold"),
            persist=False,
        )

    def _set_thresholds(
        self,
        match_threshold: object,
        margin_threshold: object,
        *,
        persist: bool,
    ) -> dict[str, float]:
        if (
            isinstance(match_threshold, bool)
            or not isinstance(match_threshold, (int, float))
            or not -1.0 <= float(match_threshold) <= 1.0
        ):
            raise SpeakerIdentityError("match threshold must be between -1 and 1")
        if (
            isinstance(margin_threshold, bool)
            or not isinstance(margin_threshold, (int, float))
            or not 0.0 <= float(margin_threshold) <= 2.0
        ):
            raise SpeakerIdentityError("margin threshold must be between 0 and 2")
        self.match_threshold = float(match_threshold)
        self.margin_threshold = float(margin_threshold)
        value = {
            "match_threshold": self.match_threshold,
            "margin_threshold": self.margin_threshold,
        }
        if persist:
            _atomic_json(
                self.settings_path,
                {"schema_version": SETTINGS_SCHEMA_VERSION, **value},
            )
        return value

    def update_settings(self, value: object) -> dict[str, float]:
        if not isinstance(value, dict) or set(value) != {
            "match_threshold",
            "margin_threshold",
        }:
            raise SpeakerIdentityError(
                "settings require exactly match_threshold and margin_threshold"
            )
        return self._set_thresholds(
            value["match_threshold"],
            value["margin_threshold"],
            persist=True,
        )

    def settings(self) -> dict[str, float]:
        return {
            "match_threshold": self.match_threshold,
            "margin_threshold": self.margin_threshold,
        }

    def _profile_documents(
        self, *, include_disabled: bool = True
    ) -> dict[str, dict[str, object]]:
        return load_profile_documents(
            self.profiles_path,
            model_sha256=self.embedder.model_sha256,
            include_disabled=include_disabled,
        )

    @staticmethod
    def _public_profile(value: dict[str, object]) -> dict[str, object]:
        return {
            key: value.get(key)
            for key in (
                "speaker_id",
                "display_name",
                "ha_person_id",
                "ha_user_id",
                "enabled",
                "chunks",
                "created_at",
                "updated_at",
            )
        }

    def list_profiles(self) -> list[dict[str, object]]:
        return [
            self._public_profile(value) for value in self._profile_documents().values()
        ]

    def _enrollment_path(self, speaker_id: str) -> Path:
        return self.enrollments_path / f"{_validated_speaker_id(speaker_id)}.json"

    def _read_enrollment(self, speaker_id: str) -> dict[str, object]:
        path = self._enrollment_path(speaker_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise SpeakerIdentityError("speaker enrollment does not exist") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SpeakerIdentityError("speaker enrollment is invalid") from exc
        vectors = value.get("vectors") if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != ENROLLMENT_SCHEMA_VERSION
            or value.get("speaker_id") != speaker_id
            or value.get("model_sha256") != self.embedder.model_sha256
            or not isinstance(vectors, list)
            or len(vectors) > 64
            or not all(
                isinstance(vector, list)
                and vector
                and len(vector) <= MAX_EMBEDDING_DIMENSIONS
                and all(isinstance(item, (int, float)) for item in vector)
                for vector in vectors
            )
        ):
            raise SpeakerIdentityError("speaker enrollment is invalid")
        value["vectors"] = [
            _normalize([float(item) for item in vector]) for vector in vectors
        ]
        return value

    @staticmethod
    def _public_enrollment(value: dict[str, object]) -> dict[str, object]:
        vectors = value.get("vectors")
        sample_count = len(vectors) if isinstance(vectors, list) else 0
        return {
            "speaker_id": value.get("speaker_id"),
            "display_name": value.get("display_name"),
            "ha_person_id": value.get("ha_person_id"),
            "ha_user_id": value.get("ha_user_id"),
            "sample_count": sample_count,
            "required_samples": MIN_ENROLLMENT_VECTORS,
            "ready": sample_count >= MIN_ENROLLMENT_VECTORS,
            "created_at": value.get("created_at"),
            "updated_at": value.get("updated_at"),
        }

    def list_enrollments(self) -> list[dict[str, object]]:
        return [
            self._public_enrollment(self._read_enrollment(path.stem))
            for path in sorted(self.enrollments_path.glob("*.json"))
        ]

    def start_enrollment(self, value: object) -> dict[str, object]:
        if not isinstance(value, dict) or set(value) != {
            "speaker_id",
            "display_name",
            "ha_person_id",
            "ha_user_id",
            "consent",
        }:
            raise SpeakerIdentityError(
                "enrollment requires speaker_id, display_name, Home Assistant links, and consent"
            )
        if value.get("consent") is not True:
            raise SpeakerIdentityError("speaker enrollment requires explicit consent")
        if self.list_enrollments():
            raise SpeakerIdentityError(
                "finish or cancel the active speaker enrollment first"
            )
        speaker_id = _validated_speaker_id(value.get("speaker_id"))
        if speaker_id in self._profile_documents():
            raise SpeakerIdentityError("speaker profile already exists")
        path = self._enrollment_path(speaker_id)
        if path.exists():
            raise SpeakerIdentityError("speaker enrollment already exists")
        now = _utc_now()
        enrollment: dict[str, object] = {
            "schema_version": ENROLLMENT_SCHEMA_VERSION,
            "speaker_id": speaker_id,
            "model_sha256": self.embedder.model_sha256,
            "display_name": _validated_optional_text(
                value.get("display_name"),
                field="display name",
                limit=MAX_DISPLAY_NAME_CHARS,
            )
            or speaker_id,
            "ha_person_id": _validated_optional_text(
                value.get("ha_person_id"),
                field="Home Assistant person ID",
                limit=MAX_HA_ID_CHARS,
            ),
            "ha_user_id": _validated_optional_text(
                value.get("ha_user_id"),
                field="Home Assistant user ID",
                limit=MAX_HA_ID_CHARS,
            ),
            "consent_at": now,
            "created_at": now,
            "updated_at": now,
            "vectors": [],
            "source_sha256": [],
        }
        _atomic_json(path, enrollment)
        return self._public_enrollment(enrollment)

    def add_enrollment_sample(self, speaker_id: str, pcm: bytes) -> dict[str, object]:
        if (
            len(pcm) % SAMPLE_WIDTH
            or not MIN_IDENTIFY_BYTES <= len(pcm) <= MAX_IDENTIFY_BYTES
        ):
            raise SpeakerIdentityError(
                "identity audio must be 1 to 10 seconds of PCM16"
            )
        enrollment = self._read_enrollment(speaker_id)
        voiced = _voiced_only(pcm)
        if len(voiced) < MIN_VOICED_IDENTIFY_BYTES:
            result = self._public_enrollment(enrollment)
            result["accepted"] = False
            result["reason"] = "not_enough_speech"
            return result
        vector = self.embedder.embed(voiced)
        if vector is None:
            result = self._public_enrollment(enrollment)
            result["accepted"] = False
            result["reason"] = "embedding_unavailable"
            return result
        vectors = enrollment["vectors"]
        source_hashes = enrollment.get("source_sha256")
        assert isinstance(vectors, list)
        if not isinstance(source_hashes, list):
            raise SpeakerIdentityError("speaker enrollment is invalid")
        digest = hashlib.sha256(pcm).hexdigest()
        if digest in source_hashes:
            result = self._public_enrollment(enrollment)
            result["accepted"] = False
            result["reason"] = "duplicate"
            return result
        if len(vectors) >= 64:
            raise SpeakerIdentityError("speaker enrollment has too many samples")
        vectors.append(_normalize(vector))
        source_hashes.append(digest)
        enrollment["updated_at"] = _utc_now()
        _atomic_json(self._enrollment_path(speaker_id), enrollment)
        result = self._public_enrollment(enrollment)
        result["accepted"] = True
        return result

    def complete_enrollment(self, speaker_id: str) -> dict[str, object]:
        enrollment = self._read_enrollment(speaker_id)
        vectors = enrollment["vectors"]
        assert isinstance(vectors, list)
        if len(vectors) < MIN_ENROLLMENT_VECTORS:
            raise SpeakerIdentityError(
                f"speaker enrollment needs at least {MIN_ENROLLMENT_VECTORS} samples"
            )
        now = _utc_now()
        profile: dict[str, object] = {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "speaker_id": speaker_id,
            "model_sha256": self.embedder.model_sha256,
            "source_sha256": enrollment.get("source_sha256", []),
            "chunks": len(vectors),
            "centroid": _mean_normalized(vectors),
            "display_name": enrollment.get("display_name", speaker_id),
            "ha_person_id": enrollment.get("ha_person_id"),
            "ha_user_id": enrollment.get("ha_user_id"),
            # Calibration and a deliberate UI action are required before a
            # profile participates in ordinary recognition.
            "enabled": False,
            "created_at": now,
            "updated_at": now,
        }
        _atomic_json(self.profiles_path / f"{speaker_id}.json", profile)
        self._enrollment_path(speaker_id).unlink()
        return self._public_profile(profile)

    def cancel_enrollment(self, speaker_id: str) -> None:
        path = self._enrollment_path(speaker_id)
        if not path.exists():
            raise SpeakerIdentityError("speaker enrollment does not exist")
        path.unlink()

    def update_profile(self, speaker_id: str, value: object) -> dict[str, object]:
        if (
            not isinstance(value, dict)
            or not value
            or not set(value)
            <= {
                "display_name",
                "ha_person_id",
                "ha_user_id",
                "enabled",
            }
        ):
            raise SpeakerIdentityError("profile update contains unsupported fields")
        profiles = self._profile_documents()
        if speaker_id not in profiles:
            raise SpeakerIdentityError("speaker profile does not exist")
        profile = profiles[speaker_id]
        if "display_name" in value:
            profile["display_name"] = (
                _validated_optional_text(
                    value["display_name"],
                    field="display name",
                    limit=MAX_DISPLAY_NAME_CHARS,
                )
                or speaker_id
            )
        for key, field in (
            ("ha_person_id", "Home Assistant person ID"),
            ("ha_user_id", "Home Assistant user ID"),
        ):
            if key in value:
                profile[key] = _validated_optional_text(
                    value[key], field=field, limit=MAX_HA_ID_CHARS
                )
        if "enabled" in value:
            if not isinstance(value["enabled"], bool):
                raise SpeakerIdentityError("enabled must be boolean")
            profile["enabled"] = value["enabled"]
        profile["updated_at"] = _utc_now()
        _atomic_json(self.profiles_path / f"{speaker_id}.json", profile)
        return self._public_profile(profile)

    def delete_profile(self, speaker_id: str) -> None:
        speaker_id = _validated_speaker_id(speaker_id)
        path = self.profiles_path / f"{speaker_id}.json"
        if not path.exists():
            raise SpeakerIdentityError("speaker profile does not exist")
        path.unlink()

    def identify_audio(
        self, pcm: bytes, *, include_disabled: bool = False
    ) -> dict[str, object]:
        documents = self._profile_documents(include_disabled=include_disabled)
        profiles = {
            speaker_id: [float(item) for item in document["centroid"]]
            for speaker_id, document in documents.items()
        }
        result = identify(
            pcm,
            embedder=self.embedder,
            profiles=profiles,
            match_threshold=self.match_threshold,
            margin_threshold=self.margin_threshold,
        )
        speaker_id = result.get("speaker_id")
        if isinstance(speaker_id, str) and speaker_id in documents:
            profile = documents[speaker_id]
            result.update(
                {
                    "display_name": profile.get("display_name"),
                    "ha_person_id": profile.get("ha_person_id"),
                    "ha_user_id": profile.get("ha_user_id"),
                    "enabled": profile.get("enabled"),
                }
            )
        return result

    def status(self) -> dict[str, object]:
        return {
            "status": "ok",
            "profiles": self.list_profiles(),
            "enrollments": self.list_enrollments(),
            "settings": self.settings(),
            "required_samples": MIN_ENROLLMENT_VECTORS,
            "raw_audio_retained": False,
        }


def serve(  # noqa: C901 - exact authenticated route table stays local to the worker.
    *,
    host: str,
    port: int,
    token: str,
    embedder: Embedder,
    profiles_path: Path,
    match_threshold: float,
    margin_threshold: float,
) -> None:
    """Serve a small authenticated local HTTP identity endpoint."""
    if len(token) < 24 or len(token) > 512:
        raise SpeakerIdentityError("service token must contain 24 to 512 characters")
    if not 0 < port < 65_536:
        raise SpeakerIdentityError("service port must be between 1 and 65535")
    if not -1.0 <= match_threshold <= 1.0:
        raise SpeakerIdentityError("match threshold must be between -1 and 1")
    if not 0.0 <= margin_threshold <= 2.0:
        raise SpeakerIdentityError("margin threshold must be between 0 and 2")
    store = SpeakerIdentityStore(
        profiles_path,
        embedder=embedder,
        match_threshold=match_threshold,
        margin_threshold=margin_threshold,
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "ha-codex-speaker-identity/1"

        def do_GET(self) -> None:
            if not self._authorized():
                self._reply(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            path = urlsplit(self.path).path
            if path == "/health":
                self._reply(
                    HTTPStatus.OK,
                    {"status": "ok", "profiles": len(store.list_profiles())},
                )
                return
            if path == "/status":
                self._reply(HTTPStatus.OK, store.status())
                return
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        def do_POST(self) -> None:
            if not self._authorized():
                self._reply(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            try:
                if path == "/identify":
                    pcm = self._read_audio()
                    include_disabled = parsed.query == "include_disabled=true"
                    result = store.identify_audio(
                        pcm, include_disabled=include_disabled
                    )
                elif path == "/enrollments":
                    result = store.start_enrollment(self._read_json())
                elif (
                    len(parts := self._path_parts(path)) == 3
                    and parts[0] == "enrollments"
                    and parts[2] == "samples"
                ):
                    result = store.add_enrollment_sample(parts[1], self._read_audio())
                elif (
                    len(parts) == 3
                    and parts[0] == "enrollments"
                    and parts[2] == "complete"
                ):
                    result = store.complete_enrollment(parts[1])
                else:
                    self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
            except SpeakerIdentityError as exc:
                self._reply(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._reply(HTTPStatus.OK, result)

        def do_PATCH(self) -> None:
            if not self._authorized():
                self._reply(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            path = urlsplit(self.path).path
            try:
                if path == "/settings":
                    result = store.update_settings(self._read_json())
                elif (
                    len(parts := self._path_parts(path)) == 2 and parts[0] == "profiles"
                ):
                    result = store.update_profile(parts[1], self._read_json())
                else:
                    self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
            except SpeakerIdentityError as exc:
                self._reply(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._reply(HTTPStatus.OK, result)

        def do_DELETE(self) -> None:
            if not self._authorized():
                self._reply(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            path = urlsplit(self.path).path
            try:
                parts = self._path_parts(path)
                if len(parts) == 2 and parts[0] == "profiles":
                    store.delete_profile(parts[1])
                elif len(parts) == 2 and parts[0] == "enrollments":
                    store.cancel_enrollment(parts[1])
                else:
                    self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
            except SpeakerIdentityError as exc:
                self._reply(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._reply(HTTPStatus.OK, {"deleted": True})

        @staticmethod
        def _path_parts(path: str) -> list[str]:
            raw = path.strip("/").split("/") if path.strip("/") else []
            parts = [unquote(part) for part in raw]
            if any(_ID.fullmatch(part) is None for part in parts[1:2]):
                raise SpeakerIdentityError("speaker ID contains unsupported characters")
            return parts

        def _content_length(self, *, maximum: int) -> int:
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError as exc:
                raise SpeakerIdentityError(
                    "request has invalid content length"
                ) from exc
            if not 0 <= length <= maximum:
                raise SpeakerIdentityError("request body exceeds its size limit")
            return length

        def _read_audio(self) -> bytes:
            length = self._content_length(maximum=MAX_IDENTIFY_BYTES)
            if not MIN_IDENTIFY_BYTES <= length <= MAX_IDENTIFY_BYTES:
                raise SpeakerIdentityError(
                    "identity audio must be 1 to 10 seconds of PCM16"
                )
            pcm = self.rfile.read(length)
            if len(pcm) != length:
                raise SpeakerIdentityError("identity audio is truncated")
            return pcm

        def _read_json(self) -> object:
            length = self._content_length(maximum=MAX_JSON_BODY_BYTES)
            if length == 0:
                raise SpeakerIdentityError("request body must be a JSON object")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise SpeakerIdentityError("request body is truncated")
            try:
                return json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SpeakerIdentityError("request body must be JSON") from exc

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            return hmac.compare_digest(supplied, f"Bearer {token}")

        def _reply(self, status: HTTPStatus, value: object) -> None:
            body = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    HTTPServer((host, port), Handler).serve_forever()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--model-sha256", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    enroll = commands.add_parser("enroll")
    enroll.add_argument("speaker_id")
    enroll.add_argument("recordings", nargs="+", type=Path)
    enroll.add_argument("--profiles", type=Path, required=True)
    identify_command = commands.add_parser("identify")
    identify_command.add_argument("audio", type=Path)
    identify_command.add_argument("--profiles", type=Path, required=True)
    identify_command.add_argument("--match-threshold", type=float, default=0.55)
    identify_command.add_argument("--margin-threshold", type=float, default=0.08)
    server = commands.add_parser("serve")
    server.add_argument("--profiles", type=Path, required=True)
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8790)
    server.add_argument(
        "--token",
        default=os.environ.get("HA_CODEX_SPEAKER_IDENTITY_TOKEN", ""),
    )
    server.add_argument("--match-threshold", type=float, default=0.55)
    server.add_argument("--margin-threshold", type=float, default=0.08)
    return parser


def main() -> int:
    """Run enrollment, one-shot identification, or the local service."""
    args = _parser().parse_args()
    try:
        embedder = SherpaOnnxEmbedder(
            args.model,
            expected_sha256=args.model_sha256,
        )
        if args.command == "enroll":
            result = build_profile(
                args.speaker_id,
                args.recordings,
                profiles=args.profiles,
                embedder=embedder,
            )
        elif args.command == "identify":
            profiles = load_profiles(
                args.profiles,
                model_sha256=embedder.model_sha256,
            )
            result = identify(
                _read_wav(args.audio),
                embedder=embedder,
                profiles=profiles,
                match_threshold=args.match_threshold,
                margin_threshold=args.margin_threshold,
            )
        else:
            serve(
                host=args.host,
                port=args.port,
                token=args.token,
                embedder=embedder,
                profiles_path=args.profiles,
                match_threshold=args.match_threshold,
                margin_threshold=args.margin_threshold,
            )
            return 0
    except (OSError, SpeakerIdentityError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
