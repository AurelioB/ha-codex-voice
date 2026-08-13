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
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Protocol

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
PROFILE_SCHEMA_VERSION = 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class SpeakerIdentityError(ValueError):
    """Speaker enrollment, profile data, or inference input is invalid."""


class Embedder(Protocol):
    """Minimal embedding backend used by enrollment and inference."""

    @property
    def model_sha256(self) -> str: ...

    def embed(self, pcm: bytes) -> list[float] | None: ...


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
) -> dict[str, object]:
    """Build one normalized centroid from several consented recordings."""
    if _ID.fullmatch(speaker_id) is None:
        raise SpeakerIdentityError("speaker ID contains unsupported characters")
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
    }
    destination = profiles / f"{speaker_id}.json"
    temporary = profiles / f".{speaker_id}.tmp-{os.getpid()}"
    try:
        with temporary.open("x", encoding="utf-8") as target:
            temporary.chmod(0o600)
            json.dump(record, target, separators=(",", ":"), sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(destination)
        destination.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return record


def load_profiles(profiles: Path, *, model_sha256: str) -> dict[str, list[float]]:
    """Load a bounded closed set matching the active model exactly."""
    loaded: dict[str, list[float]] = {}
    try:
        paths = sorted(profiles.glob("*.json"))
    except OSError as exc:
        raise SpeakerIdentityError("cannot read speaker profiles") from exc
    if len(paths) > MAX_PROFILES:
        raise SpeakerIdentityError("too many speaker profiles")
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SpeakerIdentityError(f"invalid speaker profile: {path.name}") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != PROFILE_SCHEMA_VERSION
            or value.get("model_sha256") != model_sha256
            or not isinstance(value.get("speaker_id"), str)
            or _ID.fullmatch(value["speaker_id"]) is None
            or not isinstance(value.get("centroid"), list)
            or not all(isinstance(item, (int, float)) for item in value["centroid"])
        ):
            raise SpeakerIdentityError(f"invalid speaker profile: {path.name}")
        loaded[value["speaker_id"]] = _normalize(
            [float(item) for item in value["centroid"]]
        )
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


def serve(
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
    profiles = load_profiles(profiles_path, model_sha256=embedder.model_sha256)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ha-codex-speaker-identity/1"

        def do_GET(self) -> None:
            if self.path != "/health" or not self._authorized():
                self._reply(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            self._reply(
                HTTPStatus.OK,
                {"status": "ok", "profiles": len(profiles)},
            )

        def do_POST(self) -> None:
            if self.path != "/identify" or not self._authorized():
                self._reply(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if not MIN_IDENTIFY_BYTES <= length <= MAX_IDENTIFY_BYTES:
                self._reply(HTTPStatus.BAD_REQUEST, {"error": "invalid audio size"})
                return
            pcm = self.rfile.read(length)
            if len(pcm) != length:
                self._reply(HTTPStatus.BAD_REQUEST, {"error": "truncated audio"})
                return
            try:
                result = identify(
                    pcm,
                    embedder=embedder,
                    profiles=profiles,
                    match_threshold=match_threshold,
                    margin_threshold=margin_threshold,
                )
            except SpeakerIdentityError:
                self._reply(HTTPStatus.BAD_REQUEST, {"error": "invalid audio"})
                return
            self._reply(HTTPStatus.OK, result)

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            return hmac.compare_digest(supplied, f"Bearer {token}")

        def _reply(self, status: HTTPStatus, value: object) -> None:
            body = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
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
