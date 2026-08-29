#!/usr/bin/env python3
"""Safely prepare the pinned ThirdReality PulseAudio config for local AEC.

This tool never changes live volume or service state, and it never touches ADB.
Mutating file operations are dry runs unless ``--apply`` is supplied explicitly.
"""

from __future__ import annotations

import argparse
import os
import shlex
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

BEGIN_MARKER = "### BEGIN HA CODEX VOICE AEC (managed)"
END_MARKER = "### END HA CODEX VOICE AEC (managed)"
RAW_SOURCE_DEVICE = "hw:0,2"
RAW_SINK_DEVICE = "hw:0,1"
AEC_SOURCE_NAME = "codex_echo_cancel_source"
AEC_SINK_NAME = "codex_echo_cancel_sink"
DEFAULT_AEC_METHOD = "webrtc"
SUPPORTED_AEC_METHODS = (DEFAULT_AEC_METHOD, "speex", "adrian")
DEFAULT_AEC_SINK_VOLUME_PERCENT = 25
MIN_AEC_SINK_VOLUME_PERCENT = 1
MAX_AEC_SINK_VOLUME_PERCENT = 100
_PULSE_VOLUME_NORMAL = 65_536
_AEC_BLOCK_TEMPLATE = """{begin_marker}
# This block must remain after the raw module-alsa-source/sink definitions.
# .fail makes a missing/broken AEC module fail voice startup instead of
# permitting an unsafe full-duplex route without echo cancellation.
.fail
load-module module-echo-cancel source_master=alsa_input.hw_0_2 sink_master=alsa_output.hw_0_1 source_name={source_name} sink_name={sink_name} aec_method={aec_method} use_master_format=1
set-sink-volume {sink_name} {sink_volume_raw}
set-default-source {source_name}
set-default-sink {sink_name}
{end_marker}"""
_LEGACY_AEC_BLOCK_TEMPLATE = """{begin_marker}
# This block must remain after the raw module-alsa-source/sink definitions.
# .fail makes a missing/broken AEC module fail voice startup instead of
# permitting an unsafe full-duplex route without echo cancellation.
.fail
load-module module-echo-cancel source_master=alsa_input.hw_0_2 sink_master=alsa_output.hw_0_1 source_name={source_name} sink_name={sink_name} aec_method={aec_method} use_master_format=1
set-default-source {source_name}
set-default-sink {sink_name}
{end_marker}"""


class DeploymentError(ValueError):
    """Raised when a device file does not meet the guarded deployment contract."""


def _validate_aec_sink_volume_percent(value: object) -> int:
    if type(value) is not int or not (
        MIN_AEC_SINK_VOLUME_PERCENT <= value <= MAX_AEC_SINK_VOLUME_PERCENT
    ):
        raise DeploymentError(
            "AEC sink volume percent must be an integer from "
            f"{MIN_AEC_SINK_VOLUME_PERCENT} through {MAX_AEC_SINK_VOLUME_PERCENT}"
        )
    return value


def _parse_aec_sink_volume_percent(value: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError(
            "AEC sink volume percent must be an integer from "
            f"{MIN_AEC_SINK_VOLUME_PERCENT} through {MAX_AEC_SINK_VOLUME_PERCENT}"
        )
    try:
        return _validate_aec_sink_volume_percent(int(value, 10))
    except DeploymentError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def aec_block(
    aec_method: str = DEFAULT_AEC_METHOD,
    aec_sink_volume_percent: int = DEFAULT_AEC_SINK_VOLUME_PERCENT,
) -> str:
    """Return the exact managed block for a supported method and sink volume."""
    if aec_method not in SUPPORTED_AEC_METHODS:
        raise DeploymentError(f"unsupported AEC method: {aec_method}")
    volume_percent = _validate_aec_sink_volume_percent(aec_sink_volume_percent)
    return _AEC_BLOCK_TEMPLATE.format(
        begin_marker=BEGIN_MARKER,
        end_marker=END_MARKER,
        source_name=AEC_SOURCE_NAME,
        sink_name=AEC_SINK_NAME,
        aec_method=aec_method,
        sink_volume_raw=_PULSE_VOLUME_NORMAL * volume_percent // 100,
    )


def _legacy_aec_block(aec_method: str) -> str:
    return _LEGACY_AEC_BLOCK_TEMPLATE.format(
        begin_marker=BEGIN_MARKER,
        end_marker=END_MARKER,
        source_name=AEC_SOURCE_NAME,
        sink_name=AEC_SINK_NAME,
        aec_method=aec_method,
    )


# Keep the default constant as the reviewable WebRTC block for compatibility.
AEC_BLOCK = aec_block()
SPEEX_AEC_BLOCK = aec_block("speex")
ADRIAN_AEC_BLOCK = aec_block("adrian")
_AEC_BLOCKS = {
    (aec_method, volume_percent): aec_block(aec_method, volume_percent)
    for aec_method in SUPPORTED_AEC_METHODS
    for volume_percent in range(
        MIN_AEC_SINK_VOLUME_PERCENT, MAX_AEC_SINK_VOLUME_PERCENT + 1
    )
}
_LEGACY_AEC_BLOCKS = {
    aec_method: _legacy_aec_block(aec_method) for aec_method in SUPPORTED_AEC_METHODS
}

_MAX_DEFAULT_PA_BYTES = 1024 * 1024


def render_install(
    contents: str,
    aec_method: str = DEFAULT_AEC_METHOD,
    aec_sink_volume_percent: int = DEFAULT_AEC_SINK_VOLUME_PERCENT,
) -> tuple[str, bool]:
    """Return guarded AEC config text and whether installation is needed."""
    selected_block = aec_block(aec_method, aec_sink_volume_percent)
    begin_count = contents.count(BEGIN_MARKER)
    end_count = contents.count(END_MARKER)
    if begin_count or end_count:
        if begin_count == 1 and end_count == 1:
            for (
                installed_method,
                installed_volume_percent,
            ), installed_block in _AEC_BLOCKS.items():
                if not contents.endswith(f"\n{installed_block}\n"):
                    continue
                if (
                    installed_method == aec_method
                    and installed_volume_percent == aec_sink_volume_percent
                ):
                    return contents, False
                if installed_method == aec_method:
                    raise DeploymentError(
                        "managed AEC block uses sink volume "
                        f"{installed_volume_percent}%, not requested "
                        f"{aec_sink_volume_percent}%"
                    )
                raise DeploymentError(
                    "managed AEC block uses "
                    f"{installed_method}, not requested {aec_method}; "
                    f"installed sink volume is {installed_volume_percent}%, "
                    f"requested {aec_sink_volume_percent}%"
                )
            for installed_method, legacy_block in _LEGACY_AEC_BLOCKS.items():
                if contents.endswith(f"\n{legacy_block}\n"):
                    raise DeploymentError(
                        "legacy managed AEC block uses "
                        f"{installed_method} without a persistent sink volume; "
                        "remove it, then reinstall it to migrate"
                    )
        raise DeploymentError("managed AEC block is partial, duplicated, or modified")
    if AEC_SOURCE_NAME in contents or AEC_SINK_NAME in contents:
        raise DeploymentError("unmanaged Codex AEC endpoints already exist")

    source_line = _find_master_line(contents, "module-alsa-source", RAW_SOURCE_DEVICE)
    sink_line = _find_master_line(contents, "module-alsa-sink", RAW_SINK_DEVICE)
    if source_line is None or sink_line is None:
        raise DeploymentError("pinned raw ALSA master definitions were not found")
    # Append, rather than use default.pa.d: the pinned image includes that
    # directory before it defines these two hardware master objects.
    return f"{contents}\n{selected_block}\n", True


def render_remove(contents: str) -> tuple[str, bool]:
    """Remove only an exact installer-owned current or legacy tail block."""
    begin_count = contents.count(BEGIN_MARKER)
    end_count = contents.count(END_MARKER)
    if begin_count == 0 and end_count == 0:
        return contents, False
    if begin_count == 1 and end_count == 1:
        for block in (*_AEC_BLOCKS.values(), *_LEGACY_AEC_BLOCKS.values()):
            suffix = f"\n{block}\n"
            if contents.endswith(suffix):
                return contents[: -len(suffix)], True
    raise DeploymentError("managed AEC block is partial, duplicated, or modified")


def _find_master_line(contents: str, module: str, device: str) -> int | None:
    for line_number, line in enumerate(contents.splitlines(), start=1):
        try:
            fields = shlex.split(line, comments=True, posix=True)
        except ValueError as exc:
            raise DeploymentError(
                "default.pa contains invalid shell-like quoting"
            ) from exc
        if len(fields) < 3 or fields[:2] != ["load-module", module]:
            continue
        if f"device={device}" in fields[2:]:
            return line_number
    return None


def _read_root_config(path: Path) -> tuple[str, os.stat_result]:
    if not path.is_absolute():
        raise DeploymentError("default.pa path must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DeploymentError("default.pa could not be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise DeploymentError("default.pa must be a regular file")
        if metadata.st_uid != 0:
            raise DeploymentError("default.pa must be owned by root")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise DeploymentError("default.pa must not be group/world writable")
        if metadata.st_size > _MAX_DEFAULT_PA_BYTES:
            raise DeploymentError("default.pa exceeds the guarded size limit")
        raw = _read_bounded(descriptor, _MAX_DEFAULT_PA_BYTES)
        if len(raw) > _MAX_DEFAULT_PA_BYTES:
            raise DeploymentError("default.pa exceeds the guarded size limit")
        try:
            return raw.decode("utf-8"), metadata
        except UnicodeDecodeError as exc:
            raise DeploymentError("default.pa must be UTF-8 text") from exc
    finally:
        os.close(descriptor)


def _create_backup(path: Path, contents: bytes) -> None:
    if not path.is_absolute():
        raise DeploymentError("backup path must be absolute")
    if not path.parent.is_dir():
        raise DeploymentError("backup parent directory must already exist")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        try:
            if _read_existing_backup(path) == contents:
                return
        except OSError as exc:
            raise DeploymentError("existing backup could not be verified") from exc
        raise DeploymentError("existing backup does not match default.pa") from None
    except OSError as exc:
        raise DeploymentError("backup could not be created safely") from exc
    try:
        _write_all(descriptor, contents)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_replace(path: Path, contents: str, metadata: os.stat_result) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".codex-aec-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        encoded = contents.encode("utf-8")
        _write_all(descriptor, encoded)
        os.fchmod(descriptor, stat.S_IMODE(metadata.st_mode))
        os.fchown(descriptor, metadata.st_uid, metadata.st_gid)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()


def _read_existing_backup(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0:
            raise DeploymentError("existing backup is not a root-owned regular file")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise DeploymentError("existing backup is not root-only")
        return _read_bounded(descriptor, _MAX_DEFAULT_PA_BYTES)
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


def main() -> int:
    """Run the guarded device-side command-line helper."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "install", "remove"))
    parser.add_argument(
        "--default-pa", type=Path, default=Path("/etc/pulse/default.pa")
    )
    parser.add_argument(
        "--backup",
        type=Path,
        default=Path("/data/conf/default.pa.pre-codex-aec"),
    )
    parser.add_argument(
        "--aec-method",
        choices=SUPPORTED_AEC_METHODS,
        default=DEFAULT_AEC_METHOD,
        help="AEC engine for install/check (default: webrtc)",
    )
    parser.add_argument(
        "--aec-sink-volume-percent",
        type=_parse_aec_sink_volume_percent,
        default=DEFAULT_AEC_SINK_VOLUME_PERCENT,
        metavar="PERCENT",
        help=(
            "initial AEC sink startup volume from "
            f"{MIN_AEC_SINK_VOLUME_PERCENT} through "
            f"{MAX_AEC_SINK_VOLUME_PERCENT} percent (default: "
            f"{DEFAULT_AEC_SINK_VOLUME_PERCENT})"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the requested mutation; otherwise install/remove are dry runs",
    )
    arguments = parser.parse_args()
    past_tense = {"install": "installed", "remove": "removed"}

    contents, metadata = _read_root_config(arguments.default_pa)
    if arguments.action == "check":
        _updated, needed = render_install(
            contents,
            arguments.aec_method,
            arguments.aec_sink_volume_percent,
        )
        print(  # noqa: T201 - intentional CLI status output
            "AEC startup block is ready" if needed else "AEC startup block is installed"
        )
        return 0

    if arguments.action == "install":
        updated, changed = render_install(
            contents,
            arguments.aec_method,
            arguments.aec_sink_volume_percent,
        )
    else:
        updated, changed = render_remove(contents)
    if not changed:
        print(  # noqa: T201 - intentional CLI status output
            f"AEC startup block already {past_tense[arguments.action]}"
        )
        return 0
    if not arguments.apply:
        print(  # noqa: T201 - intentional CLI status output
            f"dry run: AEC startup block would be {past_tense[arguments.action]}"
        )
        return 0
    if arguments.action == "install":
        _create_backup(arguments.backup, contents.encode("utf-8"))
    _atomic_replace(arguments.default_pa, updated, metadata)
    print(  # noqa: T201 - intentional CLI status output
        f"AEC startup block {past_tense[arguments.action]}; no service was restarted"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeploymentError as error:
        raise SystemExit(f"refusing deployment: {error}") from error
