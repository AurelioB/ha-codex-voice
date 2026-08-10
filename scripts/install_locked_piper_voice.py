"""Install a Piper voice from a size- and digest-pinned model lock."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import BinaryIO, NoReturn
from urllib.parse import urlsplit
from urllib.request import urlopen

_BUFFER_SIZE = 64 * 1024
_MAX_LOCK_BYTES = 16 * 1024
_MAX_FILE_BYTES = 512 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 120.0
_MAX_TIMEOUT_SECONDS = 600.0
_LOCK_KEYS = {"schema_version", "voice", "revision", "files"}
_FILE_KEYS = {"filename", "url", "size_bytes", "sha256"}
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")


class LockValidationError(ValueError):
    """Raised when a model lock does not satisfy the supported schema."""


class DownloadIntegrityError(RuntimeError):
    """Raised when downloaded bytes do not match their lock entry."""


@dataclass(frozen=True, slots=True)
class LockedFile:
    """One file pinned by the model lock."""

    filename: str
    url: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ModelLock:
    """Validated schema version 1 Piper model lock."""

    voice: str
    revision: str
    files: tuple[LockedFile, ...]


def _invalid(message: str) -> NoReturn:
    raise LockValidationError(message)


def _object_with_unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _invalid(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_exact_keys(
    value: dict[str, object], expected: set[str], context: str
) -> None:
    missing = expected - value.keys()
    extra = value.keys() - expected
    if missing:
        _invalid(f"{context} is missing keys: {', '.join(sorted(missing))}")
    if extra:
        _invalid(f"{context} has unsupported keys: {', '.join(sorted(extra))}")


def _require_nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid(f"{field} must be a non-empty string without outer whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _invalid(f"{field} must not contain control characters")
    return value


def _validate_revision(value: object) -> str:
    revision = _require_nonempty_string(value, "revision")
    if (
        revision in {".", ".."}
        or any(character in revision for character in "/\\%?#")
        or PureWindowsPath(revision).drive
    ):
        _invalid("revision must be one unescaped URL path segment")
    return revision


def _validate_filename(value: object, index: int) -> str:
    filename = _require_nonempty_string(value, f"files[{index}].filename")
    if (
        filename in {".", ".."}
        or any(character in filename for character in "/\\\0")
        or PureWindowsPath(filename).drive
    ):
        _invalid(f"files[{index}].filename is unsafe")
    return filename


def _validate_url(value: object, *, revision: str, filename: str, index: int) -> str:
    url = _require_nonempty_string(value, f"files[{index}].url")
    if any(character in url for character in "?#"):
        _invalid(f"files[{index}].url must not contain a query or fragment")

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise LockValidationError(f"files[{index}].url is malformed") from error

    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname != "huggingface.co"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
    ):
        _invalid(f"files[{index}].url must use https://huggingface.co")

    # Requiring literal path segments avoids encoded separators and ambiguous
    # basename checks. Piper's pinned model paths use only unescaped segments.
    if "%" in parsed.path:
        _invalid(f"files[{index}].url must use unescaped path segments")
    segments = parsed.path.split("/")
    required_prefix = ["", "rhasspy", "piper-voices", "resolve", revision]
    if (
        segments[: len(required_prefix)] != required_prefix
        or len(segments) <= len(required_prefix)
        or any(segment in {"", ".", ".."} for segment in segments[5:])
    ):
        _invalid(f"files[{index}].url must resolve the locked piper-voices revision")
    if segments[-1] != filename:
        _invalid(f"files[{index}].url basename must match filename")
    return url


def _validate_file(value: object, *, revision: str, index: int) -> LockedFile:
    if not isinstance(value, dict):
        _invalid(f"files[{index}] must be an object")
    _require_exact_keys(value, _FILE_KEYS, f"files[{index}]")

    filename = _validate_filename(value["filename"], index)
    size_bytes = value["size_bytes"]
    if type(size_bytes) is not int or size_bytes <= 0 or size_bytes > _MAX_FILE_BYTES:
        _invalid(
            f"files[{index}].size_bytes must be an integer from 1 to {_MAX_FILE_BYTES}"
        )
    digest = value["sha256"]
    if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
        _invalid(f"files[{index}].sha256 must be a 64-character hexadecimal digest")
    url = _validate_url(value["url"], revision=revision, filename=filename, index=index)
    return LockedFile(
        filename=filename,
        url=url,
        size_bytes=size_bytes,
        sha256=digest.lower(),
    )


def load_lock(lock_path: Path) -> ModelLock:
    """Read and fully validate a schema version 1 model lock."""
    if lock_path.is_symlink() or not lock_path.is_file():
        raise LockValidationError("model lock must be a regular non-symlink file")
    if lock_path.stat().st_size > _MAX_LOCK_BYTES:
        raise LockValidationError(f"model lock must not exceed {_MAX_LOCK_BYTES} bytes")
    try:
        raw = json.loads(
            lock_path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_with_unique_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LockValidationError(f"could not read model lock: {error}") from error

    if not isinstance(raw, dict):
        _invalid("model lock must be a JSON object")
    _require_exact_keys(raw, _LOCK_KEYS, "model lock")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        _invalid("model lock schema_version must be 1")

    voice = _require_nonempty_string(raw["voice"], "voice")
    revision = _validate_revision(raw["revision"])
    raw_files = raw["files"]
    if not isinstance(raw_files, list) or not raw_files:
        _invalid("files must be a non-empty list")

    files = tuple(
        _validate_file(value, revision=revision, index=index)
        for index, value in enumerate(raw_files)
    )
    filenames = [file.filename for file in files]
    if len(filenames) != len(set(filenames)):
        _invalid("files must not contain duplicate filenames")
    return ModelLock(voice=voice, revision=revision, files=files)


def _hash_regular_file(path: Path, expected_size: int) -> str | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None

    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size != expected_size:
            return None
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            while chunk := source.read(_BUFFER_SIZE):
                digest.update(chunk)
        if os.fstat(descriptor).st_size != expected_size:
            return None
        return digest.hexdigest()
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _is_valid_target(path: Path, locked_file: LockedFile) -> bool:
    observed_digest = _hash_regular_file(path, locked_file.size_bytes)
    return observed_digest is not None and hmac.compare_digest(
        observed_digest, locked_file.sha256
    )


def _copy_verified(
    source: BinaryIO, destination: BinaryIO, locked_file: LockedFile
) -> None:
    digest = hashlib.sha256()
    received = 0
    while received < locked_file.size_bytes:
        remaining = locked_file.size_bytes - received
        chunk = source.read(min(_BUFFER_SIZE, remaining))
        if not chunk:
            raise DownloadIntegrityError(
                f"{locked_file.filename}: expected {locked_file.size_bytes} bytes, "
                f"received {received}"
            )
        if len(chunk) > remaining:
            raise DownloadIntegrityError(
                f"{locked_file.filename}: response exceeds the locked size of "
                f"{locked_file.size_bytes} bytes"
            )
        destination.write(chunk)
        digest.update(chunk)
        received += len(chunk)

    if source.read(1):
        raise DownloadIntegrityError(
            f"{locked_file.filename}: response exceeds the locked size of "
            f"{locked_file.size_bytes} bytes"
        )
    observed_digest = digest.hexdigest()
    if not hmac.compare_digest(observed_digest, locked_file.sha256):
        raise DownloadIntegrityError(
            f"{locked_file.filename}: downloaded SHA-256 does not match the lock"
        )


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _install_file(target_dir: Path, locked_file: LockedFile, *, timeout: float) -> bool:
    target = target_dir / locked_file.filename
    if _is_valid_target(target, locked_file):
        return False

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=".piper-download-",
            suffix=".tmp",
            dir=target_dir,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            with urlopen(locked_file.url, timeout=timeout) as response:
                _copy_verified(response, temporary, locked_file)
            temporary.flush()
            os.fsync(temporary.fileno())

        temporary_path.replace(target)
        temporary_path = None
        _fsync_directory(target_dir)
        return True
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def install_locked_voice(
    lock_path: Path,
    target_dir: Path,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> tuple[str, ...]:
    """Install invalid or missing locked files and return installed filenames."""
    if not 0 < timeout <= _MAX_TIMEOUT_SECONDS:
        raise ValueError(
            f"timeout must be greater than 0 and at most {_MAX_TIMEOUT_SECONDS}"
        )
    model_lock = load_lock(lock_path)
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target_dir.is_symlink() or not target_dir.is_dir():
        raise RuntimeError("target directory must be a non-symlink directory")
    installed = [
        locked_file.filename
        for locked_file in model_lock.files
        if _install_file(target_dir, locked_file, timeout=timeout)
    ]
    return tuple(installed)


def main(argv: Sequence[str] | None = None) -> int:
    """Install the locked Piper voice into the requested model directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    install_locked_voice(args.lock, args.target_dir, timeout=args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
