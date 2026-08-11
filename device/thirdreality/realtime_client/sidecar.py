"""Parent-side launcher for the isolated device WebRTC endpoint."""

from __future__ import annotations

import socket
import stat
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

try:
    from webrtc_sidecar.protocol import (
        MAX_PACKET_BYTES,
        CaptureAudio,
        ControlMessage,
        PlaybackAudio,
        ProtocolError,
        decode_packet,
        encode_capture_audio,
        encode_control,
    )
except ModuleNotFoundError as import_error:  # Repository test layout.
    if import_error.name != "webrtc_sidecar":
        raise
    from device.thirdreality.webrtc_sidecar.protocol import (
        MAX_PACKET_BYTES,
        CaptureAudio,
        ControlMessage,
        PlaybackAudio,
        ProtocolError,
        decode_packet,
        encode_capture_audio,
        encode_control,
    )

DEFAULT_PYTHON_EXECUTABLE = Path("/usr/bin/python3")
DEFAULT_RUNTIME_ROOT = Path("/data/conf/codex-webrtc")
DEFAULT_DEPENDENCY_DIRECTORY = "site-packages"
_SOCKET_BUFFER_BYTES = 64 * 1024
_MAX_DRAIN_MESSAGES = 64
_SIDECAR_UID = 65_534
_SIDECAR_GID = 65_534
_SIDECAR_ENVIRONMENT = {
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
}


class SidecarError(RuntimeError):
    """Base class for local WebRTC sidecar failures."""


class SidecarBackpressure(SidecarError):
    """Raised instead of silently dropping an IPC packet."""


class SidecarClosed(SidecarError):
    """Raised after the sidecar transport or process has ended."""


class ProcessLike(Protocol):
    """Small subprocess surface used by the launcher and its fakes."""

    def poll(self) -> int | None:
        """Return a status only after the child exits."""
        ...

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the owned child and return its status."""
        ...

    def terminate(self) -> None:
        """Request graceful termination of the owned child."""
        ...

    def kill(self) -> None:
        """Force termination of the owned child."""
        ...


@dataclass(frozen=True, slots=True)
class SidecarLayout:
    """Explicit immutable paths used by the isolated interpreter."""

    python_executable: Path = DEFAULT_PYTHON_EXECUTABLE
    runtime_root: Path = DEFAULT_RUNTIME_ROOT
    source_root: Path | None = None

    @property
    def dependency_root(self) -> Path:
        """Return the only non-stdlib dependency directory admitted by the child."""
        return self.runtime_root / DEFAULT_DEPENDENCY_DIRECTORY

    @property
    def entrypoint(self) -> Path:
        """Return the absolute stdlib-only bootstrap script path."""
        source_root = self.source_root
        if source_root is None:
            source_root = Path(__file__).resolve().parent.parent
        return source_root / "webrtc_sidecar" / "__main__.py"


PathValidator = Callable[[Path, bool], Path]
PopenFactory = Callable[..., ProcessLike]
SocketPairFactory = Callable[[], tuple[socket.socket, socket.socket]]


class WebRtcSidecarClient:
    """Own one non-blocking parent socket and its isolated child process."""

    def __init__(
        self,
        transport: socket.socket,
        process: ProcessLike,
    ) -> None:
        """Adopt one already-created process and its parent transport."""
        self._transport = transport
        self._process = process
        self._closed = False
        self._released = False
        self._transport.setblocking(False)

    @classmethod
    def launch(
        cls,
        *,
        layout: SidecarLayout | None = None,
        path_validator: PathValidator | None = None,
        popen: PopenFactory = subprocess.Popen,
        socketpair: SocketPairFactory | None = None,
    ) -> WebRtcSidecarClient:
        """Validate immutable paths and launch with no ambient Python imports."""
        selected = layout or SidecarLayout()
        validator = path_validator or validate_root_owned_path
        python_executable = validator(selected.python_executable, False)
        runtime_root = validator(selected.runtime_root, True)
        dependency_root = validator(selected.dependency_root, True)
        entrypoint = validator(selected.entrypoint, False)
        if dependency_root.parent != runtime_root:
            raise SidecarError("sidecar dependency path escaped its runtime root")

        pair_factory = socketpair or _seqpacket_socketpair
        parent_socket, child_socket = pair_factory()
        try:
            _configure_socket(parent_socket)
            _configure_socket(child_socket)
            child_fd = child_socket.fileno()
            process = popen(
                [
                    str(python_executable),
                    "-I",
                    "-S",
                    str(entrypoint),
                    "--fd",
                    str(child_fd),
                    "--runtime-root",
                    str(runtime_root),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(child_fd,),
                cwd="/",
                env=_SIDECAR_ENVIRONMENT,
                user=_SIDECAR_UID,
                group=_SIDECAR_GID,
                extra_groups=(),
                umask=0o077,
                start_new_session=True,
                shell=False,
            )
        except Exception:
            parent_socket.close()
            child_socket.close()
            raise
        child_socket.close()
        return cls(parent_socket, process)

    @property
    def process(self) -> ProcessLike:
        """Expose the narrow process handle for lifecycle integration."""
        return self._process

    @property
    def closed(self) -> bool:
        """Return whether local ownership has been released."""
        return self._closed or self._released

    def fileno(self) -> int:
        """Return the parent socket descriptor for ``select`` integration."""
        if self._closed:
            return -1
        return self._transport.fileno()

    def request_offer(self) -> None:
        """Ask the prewarmed child to create one device-owned SDP offer."""
        self._send(encode_control("create_offer"))

    def set_answer(self, sdp: str) -> None:
        """Apply the App Server SDP answer in the device peer."""
        self._send(encode_control("set_answer", sdp=sdp))

    def send_audio(
        self,
        pcm: bytes,
        *,
        sample_index: int,
        capture_monotonic_ns: int,
    ) -> None:
        """Submit capture PCM without blocking or dropping on pressure."""
        self._send(
            encode_capture_audio(
                pcm,
                sample_index=sample_index,
                capture_monotonic_ns=capture_monotonic_ns,
            )
        )

    def interrupt_response(self) -> None:
        """Fence local playback while provider server VAD handles interruption."""
        self._send(encode_control("response.interrupt"))

    def stop(self) -> None:
        """Stop the active peer while retaining the isolated child process."""
        self._send(encode_control("stop"))

    def drain_messages(
        self,
        *,
        maximum: int = _MAX_DRAIN_MESSAGES,
    ) -> list[ControlMessage | PlaybackAudio]:
        """Drain complete child packets without waiting for another packet."""
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
            raise ValueError("sidecar drain maximum must be a positive integer")
        self._ensure_open()
        messages: list[ControlMessage | PlaybackAudio] = []
        for _ in range(maximum):
            try:
                packet = self._transport.recv(MAX_PACKET_BYTES + 1)
            except BlockingIOError:
                break
            except OSError as exc:
                self._closed = True
                raise SidecarClosed("sidecar IPC receive failed") from exc
            if not packet:
                self._closed = True
                raise SidecarClosed("sidecar IPC ended")
            if len(packet) > MAX_PACKET_BYTES:
                self._closed = True
                raise SidecarError("sidecar emitted an oversized IPC packet")
            try:
                decoded = decode_packet(packet)
            except ProtocolError as exc:
                self._closed = True
                raise SidecarError("sidecar emitted an invalid IPC packet") from exc
            if isinstance(decoded, CaptureAudio):
                self._closed = True
                raise SidecarError(
                    "sidecar emitted capture audio in the wrong direction"
                )
            messages.append(decoded)
        return messages

    def close(self, *, timeout: float = 1.0) -> None:
        """End the owned child within one shared absolute deadline."""
        if self._released:
            return
        total_timeout = max(0.0, timeout)
        deadline = time.monotonic() + total_timeout

        def wait_for(fraction: float = 1.0) -> bool:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                return self._process.poll() is not None
            try:
                self._process.wait(timeout=min(remaining, total_timeout * fraction))
            except subprocess.TimeoutExpired:
                return False
            return True

        try:
            if self._process.poll() is None:
                if not self._closed:
                    with suppress(SidecarError):
                        self._send(encode_control("shutdown"))
                if not wait_for(0.5):
                    self._process.terminate()
                    if not wait_for(0.3):
                        self._process.kill()
                        wait_for()
        finally:
            self._closed = True
            self._released = True
            self._transport.close()

    def _send(self, packet: bytes) -> None:
        self._ensure_open()
        try:
            sent = self._transport.send(packet)
        except BlockingIOError as exc:
            raise SidecarBackpressure("sidecar IPC is backpressured") from exc
        except OSError as exc:
            self._closed = True
            raise SidecarClosed("sidecar IPC send failed") from exc
        if sent != len(packet):
            self._closed = True
            raise SidecarError("sidecar IPC accepted a partial packet")

    def _ensure_open(self) -> None:
        if self._closed:
            raise SidecarClosed("sidecar IPC is closed")
        return_code = self._process.poll()
        if return_code is not None:
            self._closed = True
            raise SidecarClosed(f"sidecar process exited with status {return_code}")


def validate_root_owned_path(path: Path, directory: bool) -> Path:
    """Resolve one root-owned path that cannot be replaced by other users."""
    if not path.is_absolute():
        raise SidecarError("sidecar runtime paths must be absolute")
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise SidecarError("sidecar runtime path is unavailable") from exc
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(metadata.st_mode):
        raise SidecarError("sidecar runtime path has the wrong type")
    if metadata.st_uid != 0:
        raise SidecarError("sidecar runtime path must be owned by root")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SidecarError("sidecar runtime path cannot be group/world writable")
    return resolved


def _seqpacket_socketpair() -> tuple[socket.socket, socket.socket]:
    try:
        return socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    except OSError as exc:
        raise SidecarError("AF_UNIX SOCK_SEQPACKET is unavailable") from exc


def _configure_socket(transport: socket.socket) -> None:
    transport.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _SOCKET_BUFFER_BYTES)
    transport.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _SOCKET_BUFFER_BYTES)
    transport.set_inheritable(False)
