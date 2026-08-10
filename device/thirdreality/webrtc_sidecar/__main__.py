"""Stdlib-only bootstrap for the isolated ThirdReality WebRTC sidecar."""

from __future__ import annotations

import argparse
import asyncio
import socket
import stat
import sys
from pathlib import Path


def _safe_root_path(path: Path, *, directory: bool) -> Path:
    if not path.is_absolute():
        raise RuntimeError("sidecar path is not absolute")
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(metadata.st_mode):
        raise RuntimeError("sidecar path has the wrong type")
    if metadata.st_uid != 0 or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError("sidecar path is not immutable by unprivileged users")
    return resolved


def _prepare_imports(runtime_root: Path) -> None:
    source_root = _safe_root_path(
        Path(__file__).resolve().parent.parent, directory=True
    )
    dependency_root = _safe_root_path(runtime_root / "site-packages", directory=True)
    stdlib_paths = [
        value
        for value in sys.path
        if value and "site-packages" not in value and "dist-packages" not in value
    ]
    sys.path[:] = [str(source_root), str(dependency_root), *stdlib_paths]


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--fd", required=True, type=int)
    parser.add_argument("--runtime-root", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    """Validate isolation, adopt the inherited socket, and run one peer."""
    arguments = _parse_arguments()
    if arguments.fd < 3:
        return 2
    try:
        runtime_root = _safe_root_path(arguments.runtime_root, directory=True)
        _prepare_imports(runtime_root)
        transport = socket.socket(fileno=arguments.fd)
        if transport.family != socket.AF_UNIX:
            transport.close()
            return 2
        if (
            transport.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
            != socket.SOCK_SEQPACKET
        ):
            transport.close()
            return 2
        transport.set_inheritable(False)
        from webrtc_sidecar.runtime import run_sidecar  # noqa: PLC0415

        return asyncio.run(run_sidecar(transport))
    except (ImportError, OSError, RuntimeError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
