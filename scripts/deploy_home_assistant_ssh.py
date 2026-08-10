#!/usr/bin/env python3
"""Deploy the Codex Voice integration directly to Home Assistant over SSH."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from urllib import error, parse, request

INTEGRATION_NAME = "codex_voice"
INTEGRATION_RELATIVE_PATH = Path("custom_components") / INTEGRATION_NAME

DEFAULT_PORT = 22
DEFAULT_USER = "root"
DEFAULT_IDENTITY_FILE = Path("~/.ssh/ha-codex-voice")

REMOTE_COMPONENTS_DIR = "/config/custom_components"
# Home Assistant scans every direct child of custom_components as an
# integration package. Keep all transient and rollback data outside that tree.
REMOTE_UPLOAD_PREFIX = f"/config/.{INTEGRATION_NAME}-deploy-"
REMOTE_LOCK = f"/config/.{INTEGRATION_NAME}-deploy-lock"
REMOTE_TARGET = f"{REMOTE_COMPONENTS_DIR}/{INTEGRATION_NAME}"
REMOTE_BACKUP = f"/config/.{INTEGRATION_NAME}-deploy-previous"

MAX_SOURCE_FILES = 512
MAX_ARCHIVE_MEMBERS = 768
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024

UPLOAD_TIMEOUT_SECONDS = 120.0
DEPLOY_TIMEOUT_SECONDS = 120.0
HTTP_REQUEST_TIMEOUT_SECONDS = 10.0
RESTART_WAIT_SECONDS = 180.0
RESTART_POLL_SECONDS = 2.0
RESTART_GRACE_SECONDS = 8.0

_PORTABLE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}\Z")
_USER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,31}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DEPLOYMENT_ID = re.compile(r"[0-9a-f]{32}\Z")
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")

_EXCLUDED_DIRECTORY_NAMES = {
    "__pycache__",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".secrets",
    "secrets",
}
_EXCLUDED_FILE_NAMES = {
    ".env",
    "credentials.json",
    "credentials.yaml",
    "credentials.yml",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "service-account.json",
    "token",
    "token.json",
    "tokens.json",
}
_SECRET_SUFFIXES = (
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
    ".secret",
    ".token",
)
_SECRET_DATA_SUFFIXES = {"", ".json", ".txt", ".yaml", ".yml"}

_SSH_OPTIONS = (
    "BatchMode=yes",
    "IdentitiesOnly=yes",
    "StrictHostKeyChecking=yes",
    "ConnectionAttempts=1",
    "ConnectTimeout=10",
    "ServerAliveInterval=15",
    "ServerAliveCountMax=2",
)


REMOTE_DEPLOY_SCRIPT = f"""\
set -eu
umask 077

COMPONENTS={REMOTE_COMPONENTS_DIR!r}
TARGET={REMOTE_TARGET!r}
BACKUP={REMOTE_BACKUP!r}
LOCK={REMOTE_LOCK!r}
ROLLBACK_REQUIRED=0
LOCK_ACQUIRED=0

[ "$#" -eq 3 ] || {{
    printf '%s\n' 'deployment refused: invalid deployment arguments' >&2
    exit 1
}}
DEPLOYMENT_ID=$1
EXPECTED_SHA=$2
EXPECTED_SIZE=$3
case "$DEPLOYMENT_ID" in
    ''|*[!0-9a-f]*)
        printf '%s\n' 'deployment refused: invalid deployment identifier' >&2
        exit 1
        ;;
esac
[ "${{#DEPLOYMENT_ID}}" -eq 32 ] || {{
    printf '%s\n' 'deployment refused: invalid deployment identifier' >&2
    exit 1
}}
ARCHIVE={REMOTE_UPLOAD_PREFIX!r}"$DEPLOYMENT_ID.tar.gz"
STAGING={REMOTE_UPLOAD_PREFIX!r}"$DEPLOYMENT_ID-staging"

fail() {{
    printf '%s\n' "deployment refused: $1" >&2
    exit 1
}}

cleanup() {{
    if [ "$ROLLBACK_REQUIRED" -eq 1 ] && \
        [ ! -e "$TARGET" ] && [ ! -L "$TARGET" ] && \
        [ -d "$BACKUP" ] && [ ! -L "$BACKUP" ]; then
        if ! mv -- "$BACKUP" "$TARGET"; then
            printf '%s\n' 'automatic rollback failed during cleanup' >&2
        fi
    fi
    rm -f -- "$ARCHIVE"
    if [ -e "$STAGING" ] || [ -L "$STAGING" ]; then
        rm -rf -- "$STAGING"
    fi
    if [ "$LOCK_ACQUIRED" -eq 1 ]; then
        if ! rmdir -- "$LOCK"; then
            printf '%s\n' 'deployment lock cleanup failed' >&2
        fi
    fi
}}
trap cleanup 0
trap 'exit 1' 1 2 15

case "$EXPECTED_SHA" in
    ''|*[!0-9a-f]*) fail 'invalid archive checksum' ;;
esac
[ "${{#EXPECTED_SHA}}" -eq 64 ] || fail 'invalid archive checksum'
case "$EXPECTED_SIZE" in
    ''|*[!0-9]*) fail 'invalid archive size' ;;
esac

[ -d "$COMPONENTS" ] && [ ! -L "$COMPONENTS" ] || \
    fail 'custom_components is not a safe directory'
[ -f "$ARCHIVE" ] && [ ! -L "$ARCHIVE" ] || \
    fail 'uploaded archive is not a regular file'
mkdir -m 700 -- "$LOCK" || fail 'another deployment is active or left a stale lock'
LOCK_ACQUIRED=1
command -v sha256sum >/dev/null 2>&1 || fail 'sha256sum is unavailable'

ACTUAL_SIZE=$(wc -c < "$ARCHIVE" | tr -d '[:space:]')
[ "$ACTUAL_SIZE" = "$EXPECTED_SIZE" ] || fail 'archive size mismatch'
ACTUAL_SHA=$(sha256sum "$ARCHIVE") || fail 'cannot hash uploaded archive'
ACTUAL_SHA=${{ACTUAL_SHA%% *}}
[ "$ACTUAL_SHA" = "$EXPECTED_SHA" ] || fail 'archive checksum mismatch'

if [ -e "$STAGING" ] || [ -L "$STAGING" ]; then
    rm -rf -- "$STAGING"
fi
mkdir -m 700 -- "$STAGING"
tar -xzf "$ARCHIVE" -C "$STAGING" || fail 'cannot extract archive'

CANDIDATE="$STAGING/{INTEGRATION_NAME}"
[ -d "$CANDIDATE" ] && [ ! -L "$CANDIDATE" ] || \
    fail 'archive has no integration directory'
[ -z "$(find "$STAGING" -mindepth 1 -maxdepth 1 \
    ! -name {INTEGRATION_NAME!r} -print -quit)" ] || \
    fail 'archive has unexpected top-level entries'
[ -z "$(find "$STAGING" -type l -print -quit)" ] || \
    fail 'archive contains a symbolic link'

MANIFEST="$CANDIDATE/manifest.json"
[ -f "$MANIFEST" ] && [ ! -L "$MANIFEST" ] || \
    fail 'manifest.json is missing or unsafe'
grep -Eq '"domain"[[:space:]]*:[[:space:]]*"{INTEGRATION_NAME}"' \
    "$MANIFEST" || fail 'manifest domain is invalid'

HAD_TARGET=0
if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
    [ -d "$TARGET" ] && [ ! -L "$TARGET" ] || \
        fail 'existing integration is not a safe directory'
    HAD_TARGET=1
fi
if [ -e "$BACKUP" ] || [ -L "$BACKUP" ]; then
    [ -d "$BACKUP" ] && [ ! -L "$BACKUP" ] || \
        fail 'existing backup is not a safe directory'
fi

if [ "$HAD_TARGET" -eq 1 ]; then
    if [ -e "$BACKUP" ]; then
        rm -rf -- "$BACKUP"
    fi
    ROLLBACK_REQUIRED=1
    mv -- "$TARGET" "$BACKUP" || fail 'cannot preserve existing integration'
fi

if ! mv -- "$CANDIDATE" "$TARGET"; then
    if [ "$HAD_TARGET" -eq 1 ]; then
        mv -- "$BACKUP" "$TARGET" || \
            fail 'install failed and automatic rollback failed'
    fi
    fail 'cannot install integration'
fi
ROLLBACK_REQUIRED=0

printf '%s\n' 'Codex Voice integration deployed.'
if [ "$HAD_TARGET" -eq 1 ]; then
    printf '%s\n' 'Previous integration retained at /config/.codex_voice-deploy-previous.'
fi
"""


class DeploymentError(RuntimeError):
    """Report a deployment refusal without exposing sensitive values."""


@dataclass(frozen=True, slots=True)
class ArchiveSummary:
    """Describe a validated deployment archive."""

    file_count: int
    total_size: int
    archive_size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _SourceMember:
    path: Path
    archive_name: str
    is_directory: bool
    size: int
    stat_result: os.stat_result


def _path_exists_without_following(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _absolute_without_resolving(path: Path) -> Path:
    if path.is_absolute():
        return path
    return Path.cwd() / path


def _is_excluded(parts: tuple[str, ...], *, is_directory: bool) -> bool:
    lowered_parts = tuple(part.casefold() for part in parts)
    if any(part in _EXCLUDED_DIRECTORY_NAMES for part in lowered_parts):
        return True
    if is_directory:
        return False

    name = lowered_parts[-1]
    if name in _EXCLUDED_FILE_NAMES or name.startswith(".env."):
        return True
    if name.endswith((".pyc", ".pyo", *_SECRET_SUFFIXES)):
        return True

    suffix = Path(name).suffix
    return suffix in _SECRET_DATA_SUFFIXES and any(
        marker in name for marker in ("credential", "secret", "token")
    )


def _validate_portable_parts(parts: tuple[str, ...]) -> None:
    if not parts or any(_PORTABLE_NAME.fullmatch(part) is None for part in parts):
        raise DeploymentError("integration contains a non-portable path")


def _validate_manifest_bytes(contents: bytes) -> None:
    if len(contents) > MAX_MANIFEST_BYTES:
        raise DeploymentError("manifest.json exceeds the size limit")
    try:
        manifest = json.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentError("manifest.json is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise DeploymentError("manifest.json must contain a JSON object")
    if manifest.get("domain") != INTEGRATION_NAME:
        raise DeploymentError("manifest.json has the wrong integration domain")
    if not isinstance(manifest.get("name"), str) or not manifest["name"].strip():
        raise DeploymentError("manifest.json has no integration name")
    if not isinstance(manifest.get("version"), str) or not manifest["version"].strip():
        raise DeploymentError("manifest.json has no integration version")


def _read_regular_file(path: Path, limit: int) -> bytes:
    try:
        file_stat = path.lstat()
    except FileNotFoundError as exc:
        raise DeploymentError(f"required file is missing: {path.name}") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise DeploymentError(f"required file is not regular: {path.name}")
    if file_stat.st_size > limit:
        raise DeploymentError(f"required file is too large: {path.name}")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DeploymentError(f"cannot safely read required file: {path.name}") from exc
    try:
        opened_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_dev != file_stat.st_dev
            or opened_stat.st_ino != file_stat.st_ino
            or opened_stat.st_size != file_stat.st_size
        ):
            raise DeploymentError(f"required file changed while reading: {path.name}")
        with os.fdopen(descriptor, "rb", closefd=False) as file_handle:
            contents = file_handle.read(limit + 1)
    finally:
        os.close(descriptor)
    if len(contents) > limit or len(contents) != file_stat.st_size:
        raise DeploymentError(f"required file changed while reading: {path.name}")
    return contents


def _scan_source(source: Path) -> tuple[list[_SourceMember], int, int]:
    try:
        source_stat = source.lstat()
    except FileNotFoundError as exc:
        raise DeploymentError(f"integration source is missing: {source}") from exc
    if not stat.S_ISDIR(source_stat.st_mode):
        raise DeploymentError("integration source must be a real directory")

    manifest_contents = _read_regular_file(source / "manifest.json", MAX_MANIFEST_BYTES)
    _validate_manifest_bytes(manifest_contents)

    members = [
        _SourceMember(
            path=source,
            archive_name=INTEGRATION_NAME,
            is_directory=True,
            size=0,
            stat_result=source_stat,
        )
    ]
    file_count = 0
    total_size = 0

    def visit(directory: Path, relative_parts: tuple[str, ...]) -> None:
        nonlocal file_count, total_size
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise DeploymentError("cannot scan the integration source") from exc

        for entry in entries:
            entry_path = Path(entry.path)
            parts = (*relative_parts, entry.name)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise DeploymentError(
                    "cannot inspect an integration source entry"
                ) from exc
            if stat.S_ISLNK(entry_stat.st_mode):
                raise DeploymentError("integration source contains a symbolic link")

            is_directory = stat.S_ISDIR(entry_stat.st_mode)
            if _is_excluded(parts, is_directory=is_directory):
                continue
            _validate_portable_parts(parts)

            if is_directory:
                members.append(
                    _SourceMember(
                        path=entry_path,
                        archive_name=PurePosixPath(INTEGRATION_NAME, *parts).as_posix(),
                        is_directory=True,
                        size=0,
                        stat_result=entry_stat,
                    )
                )
                if len(members) > MAX_ARCHIVE_MEMBERS:
                    raise DeploymentError("integration has too many archive entries")
                visit(entry_path, parts)
                continue

            if not stat.S_ISREG(entry_stat.st_mode):
                raise DeploymentError("integration source contains a non-regular file")
            if entry_stat.st_size > MAX_FILE_BYTES:
                raise DeploymentError("an integration file exceeds the size limit")
            file_count += 1
            total_size += entry_stat.st_size
            if file_count > MAX_SOURCE_FILES:
                raise DeploymentError("integration has too many files")
            if total_size > MAX_SOURCE_BYTES:
                raise DeploymentError("integration source exceeds the size limit")
            members.append(
                _SourceMember(
                    path=entry_path,
                    archive_name=PurePosixPath(INTEGRATION_NAME, *parts).as_posix(),
                    is_directory=False,
                    size=entry_stat.st_size,
                    stat_result=entry_stat,
                )
            )
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise DeploymentError("integration has too many archive entries")

    visit(source, ())
    return members, file_count, total_size


def _tar_info(member: _SourceMember) -> tarfile.TarInfo:
    info = tarfile.TarInfo(member.archive_name)
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = int(member.stat_result.st_mtime)
    if member.is_directory:
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        info.size = 0
    else:
        info.type = tarfile.REGTYPE
        info.mode = 0o644
        info.size = member.size
    return info


def _open_verified_member(member: _SourceMember) -> BinaryIO:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(member.path, flags)
    except OSError as exc:
        raise DeploymentError("cannot safely open an integration file") from exc
    opened_stat = os.fstat(descriptor)
    expected = member.stat_result
    if (
        not stat.S_ISREG(opened_stat.st_mode)
        or opened_stat.st_dev != expected.st_dev
        or opened_stat.st_ino != expected.st_ino
        or opened_stat.st_size != expected.st_size
    ):
        os.close(descriptor)
        raise DeploymentError("integration source changed while packaging")
    return os.fdopen(descriptor, "rb")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    bytes_read = 0
    with path.open("rb") as archive_handle:
        while chunk := archive_handle.read(128 * 1024):
            bytes_read += len(chunk)
            if bytes_read > MAX_ARCHIVE_BYTES:
                raise DeploymentError("deployment archive exceeds the size limit")
            digest.update(chunk)
    return digest.hexdigest()


def build_archive(repository_root: Path, archive_path: Path) -> ArchiveSummary:
    """Build and verify a bounded archive containing only the integration."""
    repository_root = _absolute_without_resolving(repository_root)
    archive_path = _absolute_without_resolving(archive_path)
    source = repository_root / INTEGRATION_RELATIVE_PATH
    if archive_path.is_relative_to(source):
        raise DeploymentError("deployment archive cannot be created inside the source")
    if _path_exists_without_following(archive_path):
        raise DeploymentError("refusing to overwrite an existing deployment archive")
    if not archive_path.parent.is_dir():
        raise DeploymentError("deployment archive parent directory does not exist")

    members, _file_count, _total_size = _scan_source(source)
    created = False
    try:
        with tarfile.open(
            archive_path,
            mode="x:gz",
            compresslevel=9,
            format=tarfile.PAX_FORMAT,
        ) as archive:
            created = True
            for member in members:
                info = _tar_info(member)
                if member.is_directory:
                    archive.addfile(info)
                    continue
                with _open_verified_member(member) as file_handle:
                    archive.addfile(info, file_handle)
        archive_path.chmod(0o600)
        if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise DeploymentError("deployment archive exceeds the size limit")
        return validate_archive(archive_path)
    except (OSError, tarfile.TarError) as exc:
        if isinstance(exc, DeploymentError):
            raise
        raise DeploymentError("cannot build the deployment archive") from exc
    finally:
        if created and _path_exists_without_following(archive_path):
            try:
                if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
                    archive_path.unlink()
            except OSError:
                pass


def validate_archive(archive_path: Path) -> ArchiveSummary:
    """Validate an archive's bounds, member types, paths, and manifest."""
    try:
        archive_stat = archive_path.lstat()
    except FileNotFoundError as exc:
        raise DeploymentError("deployment archive is missing") from exc
    if not stat.S_ISREG(archive_stat.st_mode):
        raise DeploymentError("deployment archive is not a regular file")
    if archive_stat.st_size <= 0 or archive_stat.st_size > MAX_ARCHIVE_BYTES:
        raise DeploymentError("deployment archive has an invalid size")

    seen: set[str] = set()
    file_count = 0
    total_size = 0
    manifest_contents: bytes | None = None
    root_directory_seen = False
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member_number, member in enumerate(archive, start=1):
                if member_number > MAX_ARCHIVE_MEMBERS:
                    raise DeploymentError("deployment archive has too many entries")
                member_path = PurePosixPath(member.name)
                if (
                    member_path.is_absolute()
                    or member.name != member_path.as_posix()
                    or not member_path.parts
                    or member_path.parts[0] != INTEGRATION_NAME
                    or any(part in {"", ".", ".."} for part in member_path.parts)
                ):
                    raise DeploymentError("deployment archive contains an unsafe path")
                if member.name in seen:
                    raise DeploymentError(
                        "deployment archive contains duplicate entries"
                    )
                seen.add(member.name)
                relative_parts = tuple(member_path.parts[1:])
                if relative_parts:
                    _validate_portable_parts(relative_parts)
                    if _is_excluded(relative_parts, is_directory=member.isdir()):
                        raise DeploymentError(
                            "deployment archive contains an excluded entry"
                        )

                if member.isdir():
                    if member.name == INTEGRATION_NAME:
                        root_directory_seen = True
                    continue
                if not member.isreg():
                    raise DeploymentError(
                        "deployment archive contains a non-regular file"
                    )
                if member.size < 0 or member.size > MAX_FILE_BYTES:
                    raise DeploymentError(
                        "deployment archive contains an oversized file"
                    )
                file_count += 1
                total_size += member.size
                if file_count > MAX_SOURCE_FILES or total_size > MAX_SOURCE_BYTES:
                    raise DeploymentError("deployment archive exceeds source bounds")
                if member.name == f"{INTEGRATION_NAME}/manifest.json":
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise DeploymentError("cannot read manifest.json from archive")
                    manifest_contents = extracted.read(MAX_MANIFEST_BYTES + 1)
    except (OSError, tarfile.TarError) as exc:
        raise DeploymentError(
            "deployment archive is not a valid gzip tar file"
        ) from exc

    if not root_directory_seen:
        raise DeploymentError("deployment archive has no integration root")
    if manifest_contents is None:
        raise DeploymentError("deployment archive has no manifest.json")
    _validate_manifest_bytes(manifest_contents)
    return ArchiveSummary(
        file_count=file_count,
        total_size=total_size,
        archive_size=archive_stat.st_size,
        sha256=_hash_file(archive_path),
    )


def _validate_host(host: str) -> str:
    if not host or len(host) > 253 or host.startswith("-") or host.endswith("."):
        raise DeploymentError("SSH host is not a valid IPv4 address or hostname")
    if all(character.isdigit() or character == "." for character in host):
        fields = host.split(".")
        if len(fields) != 4 or any(
            not field or int(field) > 255 or (len(field) > 1 and field.startswith("0"))
            for field in fields
        ):
            raise DeploymentError("SSH host is not a valid IPv4 address or hostname")
        return host
    if any(_HOST_LABEL.fullmatch(label) is None for label in host.split(".")):
        raise DeploymentError("SSH host is not a valid IPv4 address or hostname")
    return host


def _validate_user(user: str) -> str:
    if _USER_NAME.fullmatch(user) is None:
        raise DeploymentError("SSH user name is invalid")
    return user


def _validate_port(port: int) -> int:
    if not 1 <= port <= 65535:
        raise DeploymentError("SSH port must be between 1 and 65535")
    return port


def _validate_deployment_id(deployment_id: str) -> str:
    if _DEPLOYMENT_ID.fullmatch(deployment_id) is None:
        raise DeploymentError("deployment identifier is invalid")
    return deployment_id


def _remote_archive(deployment_id: str) -> str:
    return f"{REMOTE_UPLOAD_PREFIX}{_validate_deployment_id(deployment_id)}.tar.gz"


def _ssh_option_argv(identity_file: Path) -> list[str]:
    result = ["-i", str(identity_file)]
    for option in _SSH_OPTIONS:
        result.extend(("-o", option))
    return result


def build_scp_argv(
    *,
    host: str,
    user: str,
    port: int,
    identity_file: Path,
    archive_path: Path,
    deployment_id: str,
) -> list[str]:
    """Return the fixed-option SCP argument vector for the upload."""
    target = f"{_validate_user(user)}@{_validate_host(host)}"
    _validate_port(port)
    return [
        "scp",
        "-P",
        str(port),
        *_ssh_option_argv(identity_file),
        "--",
        str(archive_path),
        f"{target}:{_remote_archive(deployment_id)}",
    ]


def build_ssh_argv(
    *,
    host: str,
    user: str,
    port: int,
    identity_file: Path,
    archive_summary: ArchiveSummary,
    deployment_id: str,
) -> list[str]:
    """Return the fixed-option SSH argument vector for remote deployment."""
    target = f"{_validate_user(user)}@{_validate_host(host)}"
    _validate_port(port)
    if _SHA256.fullmatch(archive_summary.sha256) is None:
        raise DeploymentError("archive checksum is invalid")
    if not 0 < archive_summary.archive_size <= MAX_ARCHIVE_BYTES:
        raise DeploymentError("archive size is invalid")
    deployment_id = _validate_deployment_id(deployment_id)
    return [
        "ssh",
        "-p",
        str(port),
        *_ssh_option_argv(identity_file),
        "--",
        target,
        "sh",
        "-s",
        "--",
        deployment_id,
        archive_summary.sha256,
        str(archive_summary.archive_size),
    ]


def _validate_identity_file(identity_file: Path, *, required: bool) -> None:
    try:
        identity_stat = identity_file.lstat()
    except FileNotFoundError as exc:
        if required:
            raise DeploymentError("SSH identity file does not exist") from exc
        return
    if not stat.S_ISREG(identity_stat.st_mode):
        raise DeploymentError("SSH identity file must be a regular file, not a symlink")
    if identity_stat.st_mode & 0o077:
        raise DeploymentError(
            "SSH identity file permissions must not allow group or other access"
        )
    if identity_stat.st_size <= 0:
        raise DeploymentError("SSH identity file is empty")


def _validate_hass_url(value: str) -> str:
    try:
        parsed = parse.urlsplit(value)
        parsed_port = parsed.port
    except ValueError as exc:
        raise DeploymentError(
            "HASS_URL is not a valid Home Assistant base URL"
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or any(character.isspace() for character in value)
    ):
        raise DeploymentError("HASS_URL is not a valid Home Assistant base URL")
    if parsed_port is not None and not 1 <= parsed_port <= 65535:
        raise DeploymentError("HASS_URL is not a valid Home Assistant base URL")
    return parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _validate_hass_token(token: str) -> str:
    if (
        not token
        or token != token.strip()
        or "\r" in token
        or "\n" in token
        or len(token) > 16 * 1024
    ):
        raise DeploymentError("HASS_TOKEN is missing or invalid")
    return token


def _request_status(home_assistant_request: request.Request, timeout: float) -> int:
    with request.urlopen(
        home_assistant_request,
        timeout=timeout,
    ) as response:
        status_code = getattr(response, "status", None)
        if status_code is None:
            status_code = response.getcode()
        return int(status_code)


def _is_home_assistant_ready(base_url: str, token: str, timeout: float) -> bool:
    api_request = request.Request(
        f"{base_url}/api/",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        status_code = _request_status(api_request, timeout)
    except error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise DeploymentError(
                "Home Assistant rejected HASS_TOKEN while waiting for restart"
            ) from None
        if 400 <= exc.code < 500:
            raise DeploymentError(
                f"Home Assistant readiness endpoint returned HTTP {exc.code}"
            ) from None
        return False
    except (error.URLError, TimeoutError, OSError):
        return False
    return 200 <= status_code < 300


def restart_home_assistant(
    base_url: str,
    token: str,
    *,
    wait_timeout: float = RESTART_WAIT_SECONDS,
    poll_interval: float = RESTART_POLL_SECONDS,
) -> None:
    """Request a Home Assistant restart and wait boundedly for API readiness."""
    base_url = _validate_hass_url(base_url)
    token = _validate_hass_token(token)
    if wait_timeout <= 0 or poll_interval <= 0:
        raise DeploymentError("restart wait settings must be positive")

    restart_request = request.Request(
        f"{base_url}/api/services/homeassistant/restart",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        restart_status = _request_status(
            restart_request,
            min(HTTP_REQUEST_TIMEOUT_SECONDS, wait_timeout),
        )
    except error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise DeploymentError("Home Assistant rejected HASS_TOKEN") from None
        raise DeploymentError(
            f"Home Assistant restart request returned HTTP {exc.code}"
        ) from None
    except (error.URLError, TimeoutError, OSError) as exc:
        raise DeploymentError("Home Assistant restart request failed") from exc
    if not 200 <= restart_status < 300:
        raise DeploymentError(
            f"Home Assistant restart request returned HTTP {restart_status}"
        )

    deadline = time.monotonic() + wait_timeout
    maximum_attempts = max(1, math.ceil(wait_timeout / poll_interval))
    grace_seconds = min(RESTART_GRACE_SECONDS, wait_timeout / 2)
    saw_unavailable = False
    for attempt in range(1, maximum_attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))
        remaining = deadline - time.monotonic()
        request_timeout = max(
            0.1,
            min(HTTP_REQUEST_TIMEOUT_SECONDS, max(remaining, 0.1)),
        )
        ready = _is_home_assistant_ready(base_url, token, request_timeout)
        if ready and (saw_unavailable or attempt * poll_interval >= grace_seconds):
            return
        if not ready:
            saw_unavailable = True
    raise DeploymentError(
        f"Home Assistant did not become ready within {wait_timeout:g} seconds"
    )


def _run_command(
    argv: Sequence[str],
    *,
    timeout: float,
    input_text: str | None = None,
) -> None:
    try:
        if input_text is None:
            subprocess.run(
                list(argv),
                check=True,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=timeout,
            )
        else:
            subprocess.run(
                list(argv),
                check=True,
                input=input_text,
                text=True,
                timeout=timeout,
            )
    except FileNotFoundError as exc:
        raise DeploymentError(f"required executable is unavailable: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DeploymentError(f"{argv[0]} operation timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise DeploymentError(
            f"{argv[0]} operation failed with exit status {exc.returncode}"
        ) from exc


def _restart_configuration() -> tuple[str, str]:
    base_url = os.environ.get("HASS_URL")
    token = os.environ.get("HASS_TOKEN")
    if not base_url:
        raise DeploymentError("--restart requires HASS_URL")
    if not token:
        raise DeploymentError("--restart requires HASS_TOKEN")
    return _validate_hass_url(base_url), _validate_hass_token(token)


def _parse_port(value: str) -> int:
    try:
        port = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    try:
        return _validate_port(port)
    except DeploymentError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Securely deploy custom_components/codex_voice to Home Assistant over SSH."
        ),
        epilog=(
            "Configure the official Terminal & SSH app with a dedicated public "
            "key, password authentication disabled, and a trusted-network TCP "
            "port before using this command. Verify the server host key before "
            "the first deployment."
        ),
    )
    parser.add_argument(
        "--host",
        required=True,
        help="Home Assistant SSH host (required; for example homeassistant.local)",
    )
    parser.add_argument("--port", default=DEFAULT_PORT, type=_parse_port)
    parser.add_argument("--user", default=DEFAULT_USER, help="Home Assistant SSH user")
    parser.add_argument(
        "--identity-file",
        "--key",
        dest="identity_file",
        type=Path,
        default=DEFAULT_IDENTITY_FILE,
        help="dedicated SSH private key",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).parents[1],
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="restart Home Assistant through its REST API after deployment",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print sanitized operations without network changes",
    )
    return parser


def _print_dry_run(
    source: Path,
    scp_argv: Sequence[str],
    ssh_argv: Sequence[str],
    summary: ArchiveSummary,
    restart_base_url: str | None,
) -> None:
    printable_scp = list(scp_argv)
    printable_scp[-2] = "<validated-archive>"
    print("Dry run: no network or remote changes will be made.")
    print(f"Package: {source} ({summary.file_count} files, {summary.total_size} bytes)")
    print(f"Upload: {shlex.join(printable_scp)}")
    print(f"Deploy: {shlex.join(ssh_argv)} < <validated-remote-script>")
    if restart_base_url is not None:
        print(
            "Restart: POST "
            f"{restart_base_url}/api/services/homeassistant/restart "
            "(Authorization: Bearer <redacted>)"
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the deployment command and return its process exit code."""
    arguments = _argument_parser().parse_args(argv)
    try:
        host = _validate_host(arguments.host)
        user = _validate_user(arguments.user)
        port = _validate_port(arguments.port)
        identity_file = _absolute_without_resolving(
            arguments.identity_file.expanduser()
        )
        repository_root = _absolute_without_resolving(arguments.repo_root.expanduser())
        _validate_identity_file(identity_file, required=not arguments.dry_run)
        restart_config = _restart_configuration() if arguments.restart else None
        deployment_id = secrets.token_hex(16)

        with tempfile.TemporaryDirectory(prefix="ha-codex-voice-deploy-") as temp_dir:
            archive_path = Path(temp_dir) / f"{INTEGRATION_NAME}.tar.gz"
            summary = build_archive(repository_root, archive_path)
            scp_argv = build_scp_argv(
                host=host,
                user=user,
                port=port,
                identity_file=identity_file,
                archive_path=archive_path,
                deployment_id=deployment_id,
            )
            ssh_argv = build_ssh_argv(
                host=host,
                user=user,
                port=port,
                identity_file=identity_file,
                archive_summary=summary,
                deployment_id=deployment_id,
            )

            if arguments.dry_run:
                _print_dry_run(
                    repository_root / INTEGRATION_RELATIVE_PATH,
                    scp_argv,
                    ssh_argv,
                    summary,
                    restart_config[0] if restart_config else None,
                )
                return 0

            _run_command(scp_argv, timeout=UPLOAD_TIMEOUT_SECONDS)
            _run_command(
                ssh_argv,
                timeout=DEPLOY_TIMEOUT_SECONDS,
                input_text=REMOTE_DEPLOY_SCRIPT,
            )

        if restart_config is not None:
            restart_home_assistant(*restart_config)
            print("Home Assistant restart completed and its API is ready.")
        return 0  # noqa: TRY300
    except DeploymentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("error: local deployment preparation failed", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: deployment interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
