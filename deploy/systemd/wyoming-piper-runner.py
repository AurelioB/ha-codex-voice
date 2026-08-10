"""Start one integrity-locked Wyoming Piper voice with bounded CPU threading."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_SUPPORTED_VERSION = "2.3.1"
_SUPPORTED_VOICE = "es_MX-ald-medium"
_SUPPORTED_REVISION = "0622afc867cf0388684853ecdf59a498b489949d"
_SUPPORTED_LOCK_SHA256 = (
    "3ced4d8723013ad7dcb8f48d4035dc3f75534240b45dd6e8bfc43ee57287aa68"
)
_EXPECTED_FILES = {
    "es_MX-ald-medium.onnx": (
        63201294,
        "019b3803293c93e34a206dd2e53a3889209a514e786fd7144f7b70196c579b63",
    ),
    "es_MX-ald-medium.onnx.json": (
        4878,
        "5a71498158e04afc8099bfd019c7e87c68eb9d042505a2b1a87e5c1ac2b1a61d",
    ),
}

_VOICE_ENV = "HA_CODEX_TTS_VOICE"
_MODEL_DIR_ENV = "HA_CODEX_TTS_MODEL_DIR"
_MODEL_LOCK_ENV = "HA_CODEX_TTS_MODEL_LOCK"
_THREADS_ENV = "HA_CODEX_TTS_THREADS"
_DEFAULT_THREADS = 4
_MAX_THREADS = 64
_MAX_LOCK_BYTES = 16 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024


class _LockedModel:
    """Verified model state used to constrain the upstream server."""

    __slots__ = (
        "catalog",
        "config_path",
        "model_dir",
        "model_path",
        "revision",
        "voice",
    )

    def __init__(
        self,
        *,
        voice: str,
        revision: str,
        model_dir: Path,
        model_path: Path,
        config_path: Path,
        catalog: dict[str, dict[str, Any]],
    ) -> None:
        """Store verified paths and the restricted catalog."""
        self.voice = voice
        self.revision = revision
        self.model_dir = model_dir
        self.model_path = model_path
        self.config_path = config_path
        self.catalog = catalog


def _get_thread_count() -> int:
    """Return the configured, bounded ONNX Runtime inference thread count."""
    raw_thread_count = os.environ.get(_THREADS_ENV, str(_DEFAULT_THREADS))
    try:
        thread_count = int(raw_thread_count)
    except ValueError as err:
        raise ValueError(
            f"{_THREADS_ENV} must be an integer from 1 to {_MAX_THREADS}"
        ) from err

    if not 1 <= thread_count <= _MAX_THREADS:
        raise ValueError(f"{_THREADS_ENV} must be an integer from 1 to {_MAX_THREADS}")

    return thread_count


def _required_absolute_path(variable: str) -> Path:
    """Load one required absolute path from the environment."""
    raw_path = os.environ.get(variable)
    if not raw_path:
        raise RuntimeError(f"{variable} is required")

    path = Path(raw_path)
    if not path.is_absolute():
        raise RuntimeError(f"{variable} must be an absolute path")

    return path


def _sha256_file(path: Path) -> str:
    """Hash a regular file without loading it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        while chunk := input_file.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_file(path: Path, size_bytes: int, expected_sha256: str) -> None:
    """Fail closed unless one model file has the exact locked identity."""
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"locked Piper file is missing or unsafe: {path}")
    if path.stat().st_size != size_bytes:
        raise RuntimeError(f"locked Piper file has the wrong size: {path}")
    if _sha256_file(path) != expected_sha256:
        raise RuntimeError(f"locked Piper file has the wrong SHA-256: {path}")


def _load_lock(lock_path: Path) -> dict[str, Any]:
    """Load the single reviewed lock asset and reject any altered copy."""
    if lock_path.is_symlink() or not lock_path.is_file():
        raise RuntimeError(f"Piper model lock is missing or unsafe: {lock_path}")
    if lock_path.stat().st_size > _MAX_LOCK_BYTES:
        raise RuntimeError(f"Piper model lock is too large: {lock_path}")

    lock_bytes = lock_path.read_bytes()
    if hashlib.sha256(lock_bytes).hexdigest() != _SUPPORTED_LOCK_SHA256:
        raise RuntimeError(
            f"Piper model lock failed integrity verification: {lock_path}"
        )

    try:
        lock = json.loads(lock_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise RuntimeError(f"Piper model lock is invalid: {lock_path}") from err
    if not isinstance(lock, dict):
        raise TypeError(f"Piper model lock is invalid: {lock_path}")
    return lock


def _locked_catalog(lock: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build the only catalog entry the reviewed upstream server may expose."""
    revision_marker = f"/resolve/{_SUPPORTED_REVISION}/"
    catalog_files: dict[str, dict[str, Any]] = {}
    for file_info in lock["files"]:
        remote_path = urlsplit(file_info["url"]).path.partition(revision_marker)[2]
        if not remote_path:
            raise RuntimeError("Piper model lock contains an invalid revision URL")
        catalog_files[remote_path] = {"size_bytes": file_info["size_bytes"]}

    return {
        _SUPPORTED_VOICE: {
            "aliases": [],
            "files": catalog_files,
            "key": _SUPPORTED_VOICE,
            "language": {
                "code": "es_MX",
                "country_english": "Mexico",
                "family": "es",
                "name_english": "Spanish",
                "name_native": "Español",
                "region": "MX",
            },
            "name": "ald",
            "num_speakers": 1,
            "quality": "medium",
            "speaker_id_map": {},
        }
    }


def _load_and_verify_locked_model() -> _LockedModel:
    """Verify the immutable lock and both pinned voice files at process start."""
    lock_path = _required_absolute_path(_MODEL_LOCK_ENV)
    model_dir = _required_absolute_path(_MODEL_DIR_ENV)
    if model_dir.is_symlink() or not model_dir.is_dir():
        raise RuntimeError(f"Piper model directory is missing or unsafe: {model_dir}")

    lock = _load_lock(lock_path)
    if (
        lock.get("schema_version") != 1
        or lock.get("voice") != _SUPPORTED_VOICE
        or lock.get("revision") != _SUPPORTED_REVISION
        or not isinstance(lock.get("files"), list)
    ):
        raise RuntimeError("unsupported Piper model lock")

    locked_files: dict[str, Path] = {}
    for file_info in lock["files"]:
        if not isinstance(file_info, dict):
            raise TypeError("invalid Piper model lock file entry")
        filename = file_info.get("filename")
        expected = _EXPECTED_FILES.get(filename)
        if (
            expected is None
            or file_info.get("size_bytes") != expected[0]
            or file_info.get("sha256") != expected[1]
            or filename in locked_files
        ):
            raise RuntimeError("unsupported Piper model lock file entry")
        model_path = model_dir / filename
        _verify_file(model_path, expected[0], expected[1])
        locked_files[filename] = model_path

    if set(locked_files) != set(_EXPECTED_FILES):
        raise RuntimeError("Piper model lock must contain exactly the reviewed files")

    return _LockedModel(
        voice=_SUPPORTED_VOICE,
        revision=_SUPPORTED_REVISION,
        model_dir=model_dir,
        model_path=locked_files[f"{_SUPPORTED_VOICE}.onnx"],
        config_path=locked_files[f"{_SUPPORTED_VOICE}.onnx.json"],
        catalog=_locked_catalog(lock),
    )


def _same_path(path: str | Path, expected: Path) -> bool:
    """Compare a server-supplied path with the configured model directory."""
    return Path(path).resolve(strict=False) == expected.resolve(strict=False)


def _restrict_downloads(download: Any, locked_model: _LockedModel) -> None:
    """Replace upstream catalog/download helpers with one offline locked voice."""

    def get_voices(
        download_dir: str | Path, update_voices: bool = False
    ) -> dict[str, dict[str, Any]]:
        if update_voices:
            raise RuntimeError("Piper catalog updates are disabled")
        if not _same_path(download_dir, locked_model.model_dir):
            raise RuntimeError("Piper download directory must match the locked model")
        return locked_model.catalog

    def find_voice(name: str, data_dirs: Iterable[str | Path]) -> tuple[Path, Path]:
        if name != locked_model.voice:
            raise RuntimeError(f"unsupported Piper voice: {name}")
        if not any(_same_path(path, locked_model.model_dir) for path in data_dirs):
            raise RuntimeError("Piper data directory must contain the locked model")
        return locked_model.model_path, locked_model.config_path

    def ensure_voice_exists(
        name: str,
        data_dirs: Iterable[str | Path],
        download_dir: str | Path,
        voices_info: dict[str, Any],
    ) -> None:
        if set(voices_info) != {locked_model.voice}:
            raise RuntimeError("Piper catalog contains an unsupported voice")
        if not _same_path(download_dir, locked_model.model_dir):
            raise RuntimeError("Piper download directory must match the locked model")
        find_voice(name, data_dirs)

    download.get_voices = get_voices
    download.find_voice = find_voice
    download.ensure_voice_exists = ensure_voice_exists


def main() -> None:
    """Verify and constrain the reviewed Piper runtime, then start the server."""
    package = import_module("wyoming_piper")
    if package.__version__ != _SUPPORTED_VERSION:
        raise RuntimeError(f"unsupported wyoming-piper version: {package.__version__}")

    locked_model = _load_and_verify_locked_model()
    configured_voice = os.environ.get(_VOICE_ENV)
    if configured_voice != locked_model.voice:
        raise RuntimeError(
            f"{_VOICE_ENV} must be the locked voice {locked_model.voice}"
        )

    thread_count = _get_thread_count()
    onnxruntime = import_module("onnxruntime")
    real_session_options = onnxruntime.SessionOptions

    def configured_session_options():
        options = real_session_options()
        options.intra_op_num_threads = thread_count
        options.inter_op_num_threads = 1
        return options

    onnxruntime.SessionOptions = configured_session_options
    download = import_module("wyoming_piper.download")
    _restrict_downloads(download, locked_model)
    server = import_module("wyoming_piper.__main__")
    server.run()


if __name__ == "__main__":
    main()
