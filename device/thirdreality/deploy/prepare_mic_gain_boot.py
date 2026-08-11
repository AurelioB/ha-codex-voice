#!/usr/bin/env python3
"""Safely install the ThirdReality early microphone-gain boot hook.

The pinned firmware applies ``sound.json`` microphone gain after PulseAudio has
already opened the PDM capture device.  This helper installs one earlier init
script and never edits a vendor init script, changes live mixer state, restarts
a service, or touches ADB.  Mutations are dry runs unless ``--apply`` is given.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

HOOK_NAME = "S49codex-mic-gain"
PULSEAUDIO_INIT_NAME = "S50pulseaudio"
DEFAULT_HOOK_PATH = Path("/etc/init.d") / HOOK_NAME
DEFAULT_PULSEAUDIO_INIT_PATH = Path("/etc/init.d") / PULSEAUDIO_INIT_NAME
DEFAULT_RC_STARTUP_PATH = Path("/etc/init.d/rcS")
PINNED_PULSEAUDIO_INIT_SHA256 = (
    "4fc2585363aefce1906e93d33e1bc98acfc29d4d2deda4be440fafc36093ce8e"
)
PINNED_RC_STARTUP_SHA256 = (
    "d8c1c87f8c12f57d2b03152530d13ac1645af1e2a378b8f84be2d1a682702a45"
)
HOOK_MODE = 0o755
_MAX_INIT_BYTES = 64 * 1024

MIC_GAIN_BOOT_SCRIPT = """#!/bin/sh
# HA Codex Voice managed early microphone-gain hook.
# PulseAudio must open hw:0,2 only after this mixer write has completed.

SOUND_CONF=/data/conf/sound.json
DEFAULT_MIC_GAIN=30

read_mic_gain() {
    if [ -f "$SOUND_CONF" ] && [ ! -L "$SOUND_CONF" ]; then
        value=$(/usr/bin/jq -er '
            if type == "object" and has("mic_gain") then .mic_gain else empty end
            | select(type == "number")
            | select(. >= 0 and . <= 100)
            | select(. == floor)
            | if . == 0 then 0 else floor end
        ' "$SOUND_CONF" 2>/dev/null) || value=
        case "$value" in
            ''|*[!0-9]*) ;;
            *)
                printf '%s\\n' "$value"
                return 0
                ;;
        esac
    fi

    /usr/bin/logger -t codex-mic-gain \\
        "invalid or absent sound.json gain; using 30%" 2>/dev/null || :
    printf '%s\\n' "codex-mic-gain: invalid or absent sound.json gain; using 30%" >&2
    printf '%s\\n' "$DEFAULT_MIC_GAIN"
}

case "${1:-}" in
    start)
        MIC_GAIN=$(read_mic_gain)
        if ! /usr/bin/amixer -c 0 cset numid=7 "${MIC_GAIN}%" >/dev/null 2>&1; then
            printf '%s\\n' "codex-mic-gain: failed to apply PDM gain before PulseAudio" >&2
            exit 1
        fi
        /usr/bin/logger -t codex-mic-gain \\
            "Applied pre-PulseAudio microphone gain: ${MIC_GAIN}%" 2>/dev/null || :
        printf 'Applied pre-PulseAudio microphone gain: %s%%\\n' "$MIC_GAIN"
        ;;
    stop)
        ;;
    *)
        printf 'Usage: %s {start|stop}\\n' "$0" >&2
        exit 1
        ;;
esac
"""


class DeploymentError(ValueError):
    """Raised when the device does not meet the guarded deployment contract."""


def render_install(contents: str | None) -> tuple[str, bool]:
    """Return the managed hook and whether installation is needed."""
    if contents is None:
        return MIC_GAIN_BOOT_SCRIPT, True
    if contents == MIC_GAIN_BOOT_SCRIPT:
        return contents, False
    raise DeploymentError("mic-gain boot hook already exists with unknown contents")


def render_remove(contents: str | None) -> tuple[str | None, bool]:
    """Remove only the exact installer-owned hook."""
    if contents is None:
        return None, False
    if contents == MIC_GAIN_BOOT_SCRIPT:
        return None, True
    raise DeploymentError("mic-gain boot hook is modified or not installer-owned")


def _validate_paths(hook_path: Path, pulseaudio_init_path: Path) -> None:
    if not hook_path.is_absolute() or not pulseaudio_init_path.is_absolute():
        raise DeploymentError("init-script paths must be absolute")
    if hook_path.name != HOOK_NAME:
        raise DeploymentError(f"hook path must end in {HOOK_NAME}")
    if pulseaudio_init_path.name != PULSEAUDIO_INIT_NAME:
        raise DeploymentError(
            f"PulseAudio init path must end in {PULSEAUDIO_INIT_NAME}"
        )
    if hook_path.parent != pulseaudio_init_path.parent:
        raise DeploymentError("mic-gain and PulseAudio init scripts must be siblings")
    if hook_path.name >= pulseaudio_init_path.name:
        raise DeploymentError("mic-gain hook must sort before PulseAudio startup")


def _validate_pinned_boot(
    hook_path: Path,
    pulseaudio_init_path: Path,
    rc_startup_path: Path,
) -> None:
    """Fail closed unless the known firmware boot ordering is present."""
    _validate_paths(hook_path, pulseaudio_init_path)
    try:
        pulseaudio_init, _metadata = _read_secure_regular(
            pulseaudio_init_path,
            expected_mode=HOOK_MODE,
        )
        rc_startup, _metadata = _read_secure_regular(
            rc_startup_path,
            expected_mode=HOOK_MODE,
        )
    except FileNotFoundError as exc:
        raise DeploymentError("pinned firmware boot files were not found") from exc
    if _sha256(pulseaudio_init) != PINNED_PULSEAUDIO_INIT_SHA256:
        raise DeploymentError("PulseAudio init script does not match pinned firmware")
    if _sha256(rc_startup) != PINNED_RC_STARTUP_SHA256:
        raise DeploymentError("startup runner does not match pinned firmware")


def _read_optional_hook(path: Path) -> tuple[str | None, os.stat_result | None]:
    try:
        raw, metadata = _read_secure_regular(path, expected_mode=HOOK_MODE)
    except FileNotFoundError:
        return None, None
    try:
        return raw.decode("utf-8"), metadata
    except UnicodeDecodeError as exc:
        raise DeploymentError("mic-gain boot hook must be UTF-8 text") from exc


def _read_secure_regular(
    path: Path,
    *,
    expected_mode: int,
) -> tuple[bytes, os.stat_result]:
    if not path.is_absolute():
        raise DeploymentError("device path must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise FileNotFoundError(path) from exc
        raise DeploymentError(f"could not open {path} safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise DeploymentError(f"{path} must be a regular file")
        if metadata.st_uid != 0:
            raise DeploymentError(f"{path} must be owned by root")
        if stat.S_IMODE(metadata.st_mode) != expected_mode:
            raise DeploymentError(f"{path} must have mode {expected_mode:04o}")
        if metadata.st_size > _MAX_INIT_BYTES:
            raise DeploymentError(f"{path} exceeds the guarded size limit")
        return _read_bounded(descriptor, _MAX_INIT_BYTES), metadata
    finally:
        os.close(descriptor)


def _validate_parent(path: Path) -> None:
    try:
        metadata = path.parent.stat(follow_symlinks=False)
    except OSError as exc:
        raise DeploymentError("init-script parent could not be inspected") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise DeploymentError("init-script parent must be a directory")
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise DeploymentError(
            "init-script parent must be root-owned and not group/world writable"
        )


def _atomic_install(path: Path, contents: str) -> None:
    _validate_parent(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".codex-mic-gain-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        _write_all(descriptor, contents.encode("utf-8"))
        os.fchmod(descriptor, HOOK_MODE)
        os.fchown(descriptor, 0, 0)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if path.exists() or path.is_symlink():
            raise DeploymentError("mic-gain boot hook appeared during installation")
        temporary.replace(path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()


def _remove(path: Path) -> None:
    path.unlink()
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_bounded(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    value = b"".join(chunks)
    if len(value) > maximum:
        raise DeploymentError("file exceeds the guarded size limit")
    return value


def _write_all(descriptor: int, value: bytes) -> None:
    remaining = memoryview(value)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise DeploymentError("file write did not make progress")
        remaining = remaining[written:]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def main() -> int:
    """Run the guarded device-side installer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "install", "remove"))
    parser.add_argument("--hook", type=Path, default=DEFAULT_HOOK_PATH)
    parser.add_argument(
        "--pulseaudio-init",
        type=Path,
        default=DEFAULT_PULSEAUDIO_INIT_PATH,
    )
    parser.add_argument(
        "--rc-startup",
        type=Path,
        default=DEFAULT_RC_STARTUP_PATH,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform install/remove; otherwise both actions are dry runs",
    )
    arguments = parser.parse_args()

    if arguments.action == "remove":
        # Keep exact target scoping even when firmware drift makes the pinned
        # boot hash unavailable. Rollback must remain possible after an update.
        _validate_paths(arguments.hook, arguments.pulseaudio_init)
    else:
        _validate_pinned_boot(
            arguments.hook,
            arguments.pulseaudio_init,
            arguments.rc_startup,
        )
    contents, _metadata = _read_optional_hook(arguments.hook)

    if arguments.action == "check":
        _updated, changed = render_install(contents)
        print(  # noqa: T201 - intentional CLI status output
            "mic-gain boot hook is ready"
            if changed
            else "mic-gain boot hook is installed"
        )
        return 0

    if arguments.action == "install":
        updated, changed = render_install(contents)
        past_tense = "installed"
    else:
        _removed, changed = render_remove(contents)
        updated = None
        past_tense = "removed"

    if not changed:
        print(  # noqa: T201 - intentional CLI status output
            f"mic-gain boot hook already {past_tense}"
        )
        return 0
    if not arguments.apply:
        print(  # noqa: T201 - intentional CLI status output
            f"dry run: mic-gain boot hook would be {past_tense}"
        )
        return 0
    if os.geteuid() != 0:
        raise DeploymentError("applying mic-gain boot changes requires root")

    if arguments.action == "install":
        assert updated is not None
        _atomic_install(arguments.hook, updated)
    else:
        _remove(arguments.hook)
    print(  # noqa: T201 - intentional CLI status output
        f"mic-gain boot hook {past_tense}; no service was restarted"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeploymentError as error:
        raise SystemExit(f"refusing deployment: {error}") from error
