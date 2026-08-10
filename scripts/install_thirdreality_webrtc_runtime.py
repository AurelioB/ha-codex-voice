"""Build and install the pinned ThirdReality Python WebRTC runtime."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import IO, Any, NoReturn

PYTHON_VERSION = "3.11"
PYTHON_PLATFORM = "aarch64-manylinux_2_28"
DEPENDENCY_DIRECTORY = "site-packages"
MANIFEST_NAME = "runtime-manifest.json"
DEFAULT_TARGET_LINK = Path("/data/conf/codex-webrtc")
DEFAULT_PYTHON = Path("/usr/bin/python3")

_EXPECTED_PACKAGES = {
    "aioice": "0.10.2",
    "aiortc": "1.15.0",
    "av": "17.1.0",
    "cffi": "2.1.1",
    "cryptography": "50.0.0",
    "dnspython": "2.8.0",
    "google-crc32c": "1.8.0",
    "ifaddr": "0.2.0",
    "pycparser": "3.0",
    "pyee": "13.0.1",
    "pylibsrtp": "1.0.0",
    "pyopenssl": "26.4.0",
    "typing-extensions": "4.16.0",
}
_MANIFEST_KEYS = {
    "schema_version",
    "python_version",
    "python_platform",
    "requirements_lock_sha256",
    "packages",
    "files",
    "total_size_bytes",
}
_FILE_KEYS = {"path", "size_bytes", "sha256"}
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_REQUIREMENT_PATTERN = re.compile(
    r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)((?:\s+--hash=sha256:[0-9a-f]{64})+)"
)
_HASH_OPTION_PATTERN = re.compile(r"--hash=sha256:([0-9a-f]{64})")
_MAX_LOCK_BYTES = 128 * 1024
_MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
_MAX_EXTRACTED_BYTES = 192 * 1024 * 1024
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_FILES = 4096
_MIN_FREE_HEADROOM_BYTES = 8 * 1024 * 1024
_BUFFER_SIZE = 1024 * 1024
_SMOKE_TIMEOUT_SECONDS = 30.0
_RUNTIME_DIRECTORY_MODE = 0o755
_RUNTIME_FILE_MODE = 0o644
_SIDECAR_UID = 65_534
_SIDECAR_GID = 65_534
_BUILD_ENVIRONMENT_KEYS = {
    "ALL_PROXY",
    "CURL_CA_BUNDLE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "all_proxy",
    "https_proxy",
    "http_proxy",
    "no_proxy",
}

_SMOKE_CODE = """
import asyncio
import platform
import sys

if sys.version_info[:2] != (3, 11):
    raise SystemExit("wrong Python version")
if platform.machine().lower() not in {"aarch64", "arm64"}:
    raise SystemExit("wrong machine architecture")
libc_name, libc_version = platform.libc_ver()
if libc_name != "glibc" or tuple(map(int, libc_version.split(".")[:2])) < (2, 28):
    raise SystemExit("glibc 2.28 or newer is required")
sys.path.insert(0, sys.argv[1])

import aiortc
import av
import pylibsrtp
from aiortc import RTCPeerConnection

if aiortc.__version__ != "1.15.0" or av.__version__ != "17.1.0":
    raise SystemExit("runtime version mismatch")
if pylibsrtp.__version__ != "1.0.0":
    raise SystemExit("SRTP runtime version mismatch")

async def smoke():
    peer = RTCPeerConnection()
    try:
        peer.addTransceiver("audio", direction="sendrecv")
        peer.createDataChannel("oai-events")
        await peer.setLocalDescription(await peer.createOffer())
        sdp = peer.localDescription.sdp
        if "m=audio " not in sdp or "m=application " not in sdp:
            raise SystemExit("offer is missing required media sections")
    finally:
        await peer.close()

asyncio.run(smoke())
""".strip()


class RuntimeValidationError(ValueError):
    """Raised when a lock, archive, or runtime fails closed validation."""


class RuntimeBuildError(RuntimeError):
    """Raised when the pinned runtime cannot be built or smoke-tested."""


@dataclass(frozen=True, slots=True)
class ManifestFile:
    """One immutable runtime file described by the bundle manifest."""

    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    """Validated schema version 1 runtime manifest."""

    requirements_lock_sha256: str
    files: tuple[ManifestFile, ...]
    total_size_bytes: int


Runner = Callable[..., subprocess.CompletedProcess[Any]]


def _invalid(message: str) -> NoReturn:
    raise RuntimeValidationError(message)


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _invalid(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _hash_file(path: Path, *, maximum_size: int | None = None) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeValidationError(f"cannot open regular file: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _invalid(f"runtime entry is not a regular file: {path}")
        if maximum_size is not None and metadata.st_size > maximum_size:
            _invalid(f"file exceeds its size limit: {path}")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            while chunk := source.read(_BUFFER_SIZE):
                digest.update(chunk)
        if os.fstat(descriptor).st_size != metadata.st_size:
            _invalid(f"file changed while hashing: {path}")
        return metadata.st_size, digest.hexdigest()
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree_directories(root: Path) -> None:
    directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
    for directory in sorted(directories, reverse=True):
        _fsync_directory(directory)


def _build_environment() -> dict[str, str]:
    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", os.defpath),
    }
    environment.update(
        (key, value)
        for key in _BUILD_ENVIRONMENT_KEYS
        if (value := os.environ.get(key)) is not None
    )
    return environment


def load_requirements_lock(lock_path: Path) -> dict[str, str]:
    """Validate the exact fully hashed requirements lock and return its pins."""
    if lock_path.is_symlink() or not lock_path.is_file():
        _invalid("requirements lock must be a regular non-symlink file")
    size, _digest = _hash_file(lock_path, maximum_size=_MAX_LOCK_BYTES)
    if size == 0:
        _invalid("requirements lock cannot be empty")
    try:
        physical_lines = lock_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RuntimeValidationError("requirements lock is not readable UTF-8") from exc

    logical_lines: list[str] = []
    partial = ""
    for raw_line in physical_lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        continuation = line.endswith("\\")
        if continuation:
            line = line[:-1].rstrip()
        partial = f"{partial} {line}".strip()
        if not continuation:
            logical_lines.append(partial)
            partial = ""
    if partial:
        _invalid("requirements lock ends with an incomplete continuation")

    pins: dict[str, str] = {}
    for line in logical_lines:
        match = _REQUIREMENT_PATTERN.fullmatch(line)
        if match is None:
            _invalid("requirements lock contains an unsupported or unhashed entry")
        name = _normalize_name(match.group(1))
        if name in pins:
            _invalid(f"requirements lock contains duplicate package: {name}")
        hashes = _HASH_OPTION_PATTERN.findall(match.group(3))
        if not hashes or len(hashes) != len(set(hashes)):
            _invalid(f"requirements lock has invalid hashes for: {name}")
        pins[name] = match.group(2)
    if pins != _EXPECTED_PACKAGES:
        _invalid("requirements lock does not match the reviewed runtime package set")
    return pins


def _installed_distributions(dependency_root: Path) -> dict[str, str]:
    packages: dict[str, str] = {}
    for metadata_path in sorted(dependency_root.glob("*.dist-info/METADATA")):
        try:
            metadata = Parser().parsestr(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise RuntimeValidationError(
                "installed package metadata is unreadable"
            ) from exc
        name = metadata.get("Name")
        version = metadata.get("Version")
        if not name or not version:
            _invalid("installed package metadata is missing Name or Version")
        normalized = _normalize_name(name)
        if normalized in packages:
            _invalid(f"runtime contains duplicate distribution metadata: {normalized}")
        packages[normalized] = version
    if packages != _EXPECTED_PACKAGES:
        _invalid("installed distributions do not match the reviewed package set")
    return packages


def _secure_tree(root: Path) -> None:
    for entry in sorted(root.rglob("*")):
        if entry.is_symlink():
            _invalid(f"runtime contains a symbolic link: {entry.relative_to(root)}")
        if entry.is_dir():
            entry.chmod(_RUNTIME_DIRECTORY_MODE)
        elif entry.is_file():
            entry.chmod(_RUNTIME_FILE_MODE)
        else:
            _invalid(f"runtime contains a special file: {entry.relative_to(root)}")
    root.chmod(_RUNTIME_DIRECTORY_MODE)


def _manifest_for(runtime_root: Path, lock_path: Path) -> bytes:
    dependency_root = runtime_root / DEPENDENCY_DIRECTORY
    packages = _installed_distributions(dependency_root)
    files: list[dict[str, object]] = []
    total_size = 0
    for path in sorted(dependency_root.rglob("*")):
        if not path.is_file():
            continue
        size, digest = _hash_file(path, maximum_size=_MAX_FILE_BYTES)
        total_size += size
        if total_size > _MAX_EXTRACTED_BYTES:
            _invalid("runtime exceeds the extracted-size limit")
        files.append(
            {
                "path": path.relative_to(runtime_root).as_posix(),
                "size_bytes": size,
                "sha256": digest,
            }
        )
    if not files or len(files) > _MAX_FILES:
        _invalid("runtime has an invalid file count")
    _lock_size, lock_digest = _hash_file(lock_path, maximum_size=_MAX_LOCK_BYTES)
    manifest = {
        "schema_version": 1,
        "python_version": PYTHON_VERSION,
        "python_platform": PYTHON_PLATFORM,
        "requirements_lock_sha256": lock_digest,
        "packages": packages,
        "files": files,
        "total_size_bytes": total_size,
    }
    return (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write_deterministic_archive(runtime_root: Path, output: IO[bytes]) -> None:
    with (
        gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed,
        tarfile.open(
            fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
        ) as archive,
    ):
        for path in [runtime_root, *sorted(runtime_root.rglob("*"))]:
            relative = path.relative_to(runtime_root)
            if not relative.parts:
                continue
            info = tarfile.TarInfo(relative.as_posix())
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            info.mtime = 0
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = _RUNTIME_DIRECTORY_MODE
                archive.addfile(info)
            else:
                size, _digest = _hash_file(path, maximum_size=_MAX_FILE_BYTES)
                info.size = size
                info.mode = _RUNTIME_FILE_MODE
                with path.open("rb") as source:
                    archive.addfile(info, source)


def build_runtime(
    lock_path: Path,
    output_archive: Path,
    *,
    uv_executable: str = "uv",
    runner: Runner = subprocess.run,
) -> str:
    """Build a deterministic aarch64 bundle and return its SHA-256 digest."""
    load_requirements_lock(lock_path)
    output_archive.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output_archive.is_symlink():
        _invalid("output archive cannot be a symbolic link")

    temporary_archive: Path | None = None
    with tempfile.TemporaryDirectory(
        prefix=".codex-webrtc-build-", dir=output_archive.parent
    ) as temporary_name:
        temporary_root = Path(temporary_name)
        runtime_root = temporary_root / "runtime"
        dependency_root = runtime_root / DEPENDENCY_DIRECTORY
        dependency_root.mkdir(mode=0o700, parents=True)
        cache_root = temporary_root / "uv-cache"
        command = [
            uv_executable,
            "pip",
            "install",
            "--requirements",
            str(lock_path.resolve(strict=True)),
            "--target",
            str(dependency_root),
            "--require-hashes",
            "--only-binary",
            ":all:",
            "--python-version",
            PYTHON_VERSION,
            "--python-platform",
            PYTHON_PLATFORM,
            "--link-mode",
            "copy",
            "--strict",
            "--default-index",
            "https://pypi.org/simple",
            "--keyring-provider",
            "disabled",
            "--no-config",
            "--no-progress",
            "--cache-dir",
            str(cache_root),
        ]
        try:
            runner(
                command,
                check=True,
                shell=False,
                stdin=subprocess.DEVNULL,
                env=_build_environment(),
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeBuildError("uv failed to install the pinned runtime") from exc

        _secure_tree(runtime_root)
        manifest_bytes = _manifest_for(runtime_root, lock_path)
        manifest_path = runtime_root / MANIFEST_NAME
        manifest_path.write_bytes(manifest_bytes)
        manifest_path.chmod(_RUNTIME_FILE_MODE)

        descriptor, archive_temporary_name = tempfile.mkstemp(
            prefix=f".{output_archive.name}.",
            suffix=".tmp",
            dir=output_archive.parent,
        )
        temporary_archive = Path(archive_temporary_name)
        try:
            with os.fdopen(descriptor, "w+b") as output:
                _write_deterministic_archive(runtime_root, output)
                output.flush()
                os.fsync(output.fileno())
            temporary_archive.chmod(0o600)
            archive_size, archive_digest = _hash_file(
                temporary_archive, maximum_size=_MAX_ARCHIVE_BYTES
            )
            if archive_size == 0:
                raise RuntimeBuildError("runtime archive is empty")
            temporary_archive.replace(output_archive)
            temporary_archive = None
            _fsync_directory(output_archive.parent)
            return archive_digest
        finally:
            if temporary_archive is not None:
                temporary_archive.unlink(missing_ok=True)


def _safe_relative_path(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        _invalid(f"{field} must be a safe relative POSIX path")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        _invalid(f"{field} must be a safe relative POSIX path")
    normalized = parsed.as_posix()
    if normalized != value:
        _invalid(f"{field} must be a normalized relative POSIX path")
    return normalized


def _load_manifest(path: Path) -> RuntimeManifest:
    size, _digest = _hash_file(path, maximum_size=1024 * 1024)
    if size == 0:
        _invalid("runtime manifest cannot be empty")
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeValidationError("runtime manifest is invalid JSON") from exc
    if not isinstance(raw, dict) or set(raw) != _MANIFEST_KEYS:
        _invalid("runtime manifest has unsupported or missing keys")
    if raw["schema_version"] != 1:
        _invalid("runtime manifest schema_version must be 1")
    if raw["python_version"] != PYTHON_VERSION:
        _invalid("runtime manifest has the wrong Python version")
    if raw["python_platform"] != PYTHON_PLATFORM:
        _invalid("runtime manifest has the wrong platform")
    if raw["packages"] != _EXPECTED_PACKAGES:
        _invalid("runtime manifest has the wrong package set")
    lock_digest = raw["requirements_lock_sha256"]
    if not isinstance(lock_digest, str) or _HASH_PATTERN.fullmatch(lock_digest) is None:
        _invalid("runtime manifest has an invalid lock digest")
    total_size = raw["total_size_bytes"]
    if type(total_size) is not int or not 0 < total_size <= _MAX_EXTRACTED_BYTES:
        _invalid("runtime manifest has an invalid total size")
    raw_files = raw["files"]
    if not isinstance(raw_files, list) or not 0 < len(raw_files) <= _MAX_FILES:
        _invalid("runtime manifest has an invalid file list")
    files: list[ManifestFile] = []
    seen: set[str] = set()
    observed_total = 0
    for index, raw_file in enumerate(raw_files):
        if not isinstance(raw_file, dict) or set(raw_file) != _FILE_KEYS:
            _invalid(f"runtime manifest files[{index}] has invalid keys")
        relative = _safe_relative_path(raw_file["path"], field=f"files[{index}].path")
        if not relative.startswith(f"{DEPENDENCY_DIRECTORY}/") or relative in seen:
            _invalid("runtime manifest contains an invalid or duplicate file path")
        seen.add(relative)
        file_size = raw_file["size_bytes"]
        digest = raw_file["sha256"]
        if type(file_size) is not int or not 0 <= file_size <= _MAX_FILE_BYTES:
            _invalid(f"runtime manifest files[{index}] has an invalid size")
        if not isinstance(digest, str) or _HASH_PATTERN.fullmatch(digest) is None:
            _invalid(f"runtime manifest files[{index}] has an invalid digest")
        observed_total += file_size
        files.append(ManifestFile(relative, file_size, digest))
    if observed_total != total_size:
        _invalid("runtime manifest total size does not match its file entries")
    return RuntimeManifest(lock_digest, tuple(files), total_size)


def _archive_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    if not members or len(members) > _MAX_FILES * 2:
        _invalid("runtime archive has an invalid member count")
    seen: set[str] = set()
    total_size = 0
    for member in members:
        name = _safe_relative_path(member.name, field="archive member")
        if name in seen:
            _invalid("runtime archive contains duplicate members")
        seen.add(name)
        if not (member.isdir() or member.isreg()):
            _invalid("runtime archive contains a link or special file")
        if member.isreg():
            if member.size < 0 or member.size > _MAX_FILE_BYTES:
                _invalid("runtime archive member exceeds its size limit")
            total_size += member.size
            if total_size > _MAX_EXTRACTED_BYTES + 1024 * 1024:
                _invalid("runtime archive exceeds its extracted-size limit")
    if MANIFEST_NAME not in seen:
        _invalid("runtime archive is missing its manifest")
    return members


def _open_destination(path: Path) -> IO[bytes]:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    return os.fdopen(descriptor, "wb")


def _extract_verified_archive(archive_path: Path, destination: Path) -> RuntimeManifest:
    if destination.is_symlink():
        _invalid("runtime staging path cannot be a symbolic link")
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            _invalid("runtime staging directory must be empty")
        destination.chmod(0o700)
    else:
        destination.mkdir(mode=0o700)
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = _archive_members(archive)
            for member in members:
                member_path = PurePosixPath(member.name)
                target = destination.joinpath(*member_path.parts)
                if member.isdir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    target.chmod(0o700)
                    continue
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    _invalid("runtime archive file has no readable content")
                with source, _open_destination(target) as output:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(_BUFFER_SIZE, remaining))
                        if not chunk:
                            _invalid("runtime archive member ended early")
                        output.write(chunk)
                        remaining -= len(chunk)
                    if source.read(1):
                        _invalid("runtime archive member exceeds its declared size")
                    output.flush()
                    os.fsync(output.fileno())
                target.chmod(0o600)
        manifest = _load_manifest(destination / MANIFEST_NAME)
        expected = {entry.path: entry for entry in manifest.files}
        observed: set[str] = set()
        for path in sorted((destination / DEPENDENCY_DIRECTORY).rglob("*")):
            if path.is_symlink() or (not path.is_file() and not path.is_dir()):
                _invalid("extracted runtime contains a link or special file")
            if path.is_dir():
                path.chmod(0o700)
                continue
            relative_path = path.relative_to(destination).as_posix()
            entry = expected.get(relative_path)
            if entry is None:
                _invalid("extracted runtime contains a file absent from its manifest")
            size, file_digest = _hash_file(path, maximum_size=_MAX_FILE_BYTES)
            if size != entry.size_bytes or not hmac.compare_digest(
                file_digest, entry.sha256
            ):
                _invalid(f"extracted runtime file failed verification: {relative_path}")
            observed.add(relative_path)
        if observed != set(expected):
            _invalid("extracted runtime is missing files from its manifest")
        _installed_distributions(destination / DEPENDENCY_DIRECTORY)
        _secure_tree(destination)
        _fsync_tree_directories(destination)
    except (OSError, tarfile.TarError) as exc:
        raise RuntimeValidationError(
            "runtime archive could not be extracted safely"
        ) from exc
    else:
        return manifest


def _verify_runtime_tree(
    runtime_root: Path,
    manifest: RuntimeManifest,
    *,
    owner_uid: int,
) -> None:
    root_metadata = runtime_root.stat()
    if (
        root_metadata.st_uid != owner_uid
        or stat.S_IMODE(root_metadata.st_mode) != _RUNTIME_DIRECTORY_MODE
    ):
        _invalid("runtime root is not immutable and sidecar-readable")
    manifest_metadata = (runtime_root / MANIFEST_NAME).stat()
    if (
        manifest_metadata.st_uid != owner_uid
        or stat.S_IMODE(manifest_metadata.st_mode) != _RUNTIME_FILE_MODE
    ):
        _invalid("runtime manifest is not immutable and sidecar-readable")
    expected = {entry.path: entry for entry in manifest.files}
    observed: set[str] = set()
    dependency_root = runtime_root / DEPENDENCY_DIRECTORY
    for path in sorted(dependency_root.rglob("*")):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            _invalid("runtime contains a link or special file")
        if path.is_dir():
            metadata = path.stat()
            if (
                metadata.st_uid != owner_uid
                or stat.S_IMODE(metadata.st_mode) != _RUNTIME_DIRECTORY_MODE
            ):
                _invalid("runtime directory is not immutable and sidecar-readable")
            continue
        relative = path.relative_to(runtime_root).as_posix()
        entry = expected.get(relative)
        if entry is None:
            _invalid("runtime contains a file absent from its manifest")
        size, digest = _hash_file(path, maximum_size=_MAX_FILE_BYTES)
        if size != entry.size_bytes or not hmac.compare_digest(digest, entry.sha256):
            _invalid(f"runtime file failed verification: {relative}")
        metadata = path.stat()
        if (
            metadata.st_uid != owner_uid
            or stat.S_IMODE(metadata.st_mode) != _RUNTIME_FILE_MODE
        ):
            _invalid("runtime file is not immutable and sidecar-readable")
        observed.add(relative)
    if observed != set(expected):
        _invalid("runtime is missing files from its manifest")
    _installed_distributions(dependency_root)


def _archive_required_free_bytes(archive_path: Path) -> int:
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            return (
                sum(
                    member.size
                    for member in _archive_members(archive)
                    if member.isreg()
                )
                + _MIN_FREE_HEADROOM_BYTES
            )
    except (OSError, tarfile.TarError) as exc:
        raise RuntimeValidationError(
            "runtime archive could not be inspected safely"
        ) from exc


def _require_secure_directory(
    path: Path, *, owner_uid: int, create: bool = False
) -> Path:
    if not path.is_absolute():
        _invalid("installation paths must be absolute")
    if path.is_symlink():
        _invalid(f"installation directory must not be a symlink: {path}")
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    if not path.is_dir():
        _invalid(f"installation directory must not be a symlink: {path}")
    metadata = path.stat()
    if metadata.st_uid != owner_uid or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        _invalid(f"installation directory is not owned securely: {path}")
    resolved = path.resolve(strict=True)
    if resolved != path:
        _invalid(f"installation directory cannot contain symbolic aliases: {path}")
    return resolved


def _verify_archive_digest(
    archive_path: Path, expected_digest: str, *, owner_uid: int
) -> str:
    if _HASH_PATTERN.fullmatch(expected_digest) is None:
        _invalid("archive SHA-256 must be 64 lowercase hexadecimal characters")
    if archive_path.is_symlink() or not archive_path.is_file():
        _invalid("runtime archive must be a regular non-symlink file")
    metadata = archive_path.stat()
    if metadata.st_uid != owner_uid or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        _invalid("runtime archive is not an immutable owner-controlled file")
    parent_metadata = archive_path.parent.stat()
    unsafe_parent = parent_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    sticky_parent = parent_metadata.st_mode & stat.S_ISVTX
    if parent_metadata.st_uid != owner_uid or (unsafe_parent and not sticky_parent):
        _invalid("runtime archive parent is not owner-controlled or sticky")
    size, digest = _hash_file(archive_path, maximum_size=_MAX_ARCHIVE_BYTES)
    if size == 0 or not hmac.compare_digest(digest, expected_digest):
        _invalid("runtime archive SHA-256 does not match")
    return digest


def _smoke_runtime(
    python_executable: Path,
    runtime_root: Path,
    *,
    owner_uid: int,
    runner: Runner,
    unprivileged_uid: int | None = None,
    unprivileged_gid: int | None = None,
) -> None:
    if not python_executable.is_absolute():
        _invalid("Python executable path must be absolute")
    try:
        resolved_python = python_executable.resolve(strict=True)
        metadata = resolved_python.stat()
    except OSError as exc:
        raise RuntimeValidationError("Python executable is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        _invalid("Python executable is not an immutable owner-controlled file")
    run_options: dict[str, Any] = {
        "check": True,
        "shell": False,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
        "timeout": _SMOKE_TIMEOUT_SECONDS,
        "cwd": "/",
        "env": {
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    }
    if unprivileged_uid is not None or unprivileged_gid is not None:
        if (
            unprivileged_uid is None
            or unprivileged_gid is None
            or unprivileged_uid <= 0
            or unprivileged_gid <= 0
        ):
            _invalid("unprivileged smoke identity is invalid")
        run_options.update(
            user=unprivileged_uid,
            group=unprivileged_gid,
            extra_groups=(),
            umask=0o077,
        )
    try:
        runner(
            [
                str(resolved_python),
                "-I",
                "-S",
                "-c",
                _SMOKE_CODE,
                str(runtime_root / DEPENDENCY_DIRECTORY),
            ],
            **run_options,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeBuildError(
            "isolated WebRTC import/offer smoke check failed"
        ) from exc


def install_runtime(
    archive_path: Path,
    expected_archive_sha256: str,
    target_link: Path = DEFAULT_TARGET_LINK,
    *,
    releases_directory: Path | None = None,
    python_executable: Path = DEFAULT_PYTHON,
    runner: Runner = subprocess.run,
    require_root: bool = True,
) -> Path:
    """Verify, smoke-test, and atomically select one immutable runtime release."""
    effective_uid = os.geteuid()
    if require_root and effective_uid != 0:
        raise PermissionError("runtime installation must run as root")
    digest = _verify_archive_digest(
        archive_path, expected_archive_sha256, owner_uid=effective_uid
    )
    target_parent = _require_secure_directory(
        target_link.parent, owner_uid=effective_uid
    )
    if target_link.parent.resolve(strict=True) != target_parent:
        _invalid("target link parent changed during validation")
    releases = releases_directory or target_parent / ".codex-webrtc-releases"
    releases = _require_secure_directory(
        releases, owner_uid=effective_uid, create=not releases.exists()
    )
    if stat.S_IMODE(releases.stat().st_mode) != _RUNTIME_DIRECTORY_MODE:
        if any(releases.iterdir()):
            _invalid("runtime releases directory must be mode 0755")
        releases.chmod(_RUNTIME_DIRECTORY_MODE)
    if releases.parent != target_parent:
        _invalid("releases directory must be an immediate child of the target parent")
    if target_link.exists() and not target_link.is_symlink():
        _invalid("target must be absent or an installer-managed symbolic link")
    if target_link.is_symlink():
        existing = str(target_link.readlink())
        prefix = f"{releases.name}/"
        if not existing.startswith(prefix) or "/" in existing[len(prefix) :]:
            _invalid("existing target link is not managed by this installer")

    release = releases / digest
    if release.exists():
        _require_secure_directory(release, owner_uid=effective_uid)
        manifest = _load_manifest(release / MANIFEST_NAME)
        _verify_runtime_tree(release, manifest, owner_uid=effective_uid)
    else:
        free_bytes = shutil.disk_usage(releases).free
        # The compressed archive already occupies disk; only its declared
        # extracted bytes and a small filesystem headroom are additionally needed.
        if free_bytes <= _archive_required_free_bytes(archive_path):
            raise RuntimeBuildError(
                "insufficient free space for staged runtime install"
            )
        staging = Path(
            tempfile.mkdtemp(prefix=f".{digest[:16]}-staging-", dir=releases)
        )
        try:
            manifest = _extract_verified_archive(archive_path, staging)
            if shutil.disk_usage(releases).free < _MIN_FREE_HEADROOM_BYTES:
                raise RuntimeBuildError(
                    "runtime install would exhaust filesystem headroom"
                )
            _smoke_runtime(
                python_executable,
                staging,
                owner_uid=effective_uid,
                runner=runner,
                unprivileged_uid=_SIDECAR_UID if require_root else None,
                unprivileged_gid=_SIDECAR_GID if require_root else None,
            )
            staging.replace(release)
            _fsync_directory(releases)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    _smoke_runtime(
        python_executable,
        release,
        owner_uid=effective_uid,
        runner=runner,
        unprivileged_uid=_SIDECAR_UID if require_root else None,
        unprivileged_gid=_SIDECAR_GID if require_root else None,
    )
    temporary_link = target_parent / f".{target_link.name}.{os.getpid()}.tmp"
    if temporary_link.exists() or temporary_link.is_symlink():
        _invalid("temporary runtime link already exists")
    try:
        temporary_link.symlink_to(f"{releases.name}/{digest}")
        temporary_link.replace(target_link)
        _fsync_directory(target_parent)
    finally:
        temporary_link.unlink(missing_ok=True)
    return release


def main(argv: Sequence[str] | None = None) -> int:
    """Build or install the pinned ThirdReality WebRTC dependency runtime."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--lock", required=True, type=Path)
    build_parser.add_argument("--output", required=True, type=Path)
    build_parser.add_argument("--uv", default="uv")

    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--archive", required=True, type=Path)
    install_parser.add_argument("--archive-sha256", required=True)
    install_parser.add_argument("--target-link", type=Path, default=DEFAULT_TARGET_LINK)
    install_parser.add_argument("--releases-dir", type=Path)
    install_parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)

    arguments = parser.parse_args(argv)
    if arguments.command == "build":
        digest = build_runtime(
            arguments.lock, arguments.output, uv_executable=arguments.uv
        )
        print(digest)
        return 0
    install_runtime(
        arguments.archive,
        arguments.archive_sha256,
        arguments.target_link,
        releases_directory=arguments.releases_dir,
        python_executable=arguments.python,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
