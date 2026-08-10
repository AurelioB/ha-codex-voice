"""Bounded in-process realtime session for the ThirdReality voice client."""

from __future__ import annotations

import json
import logging
import os
import re
import select
import shlex
import struct
import subprocess
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .config import RealtimeConfig, realtime_start_message
from .websocket import Message, WebSocketClosed, WebSocketConnection, WebSocketError

_LOGGER = logging.getLogger("linux_voice_assistant.realtime")
_INPUT_BYTES_PER_SECOND = 16_000 * 2
_INPUT_ACTIVITY_SIGNAL_PEAK = 256
_MAX_INPUT_CATCH_UP_RATE = 2.0
_PACTL_ARGV = ("/usr/bin/pactl",)
_PULSE_ECHO_CANCEL_MODULE = "module-echo-cancel"
_PULSE_SOURCE_MASTER = "alsa_input.hw_0_2"
_PULSE_SINK_MASTER = "alsa_output.hw_0_1"
_PULSE_NATIVE_DRIVER = "protocol-native.c"
_PULSE_VOLUME_RAW = re.compile(r"([0-9]+)\s*/\s*[0-9]+%\s*/")
_PULSE_VOLUME_NORM = 65_536
_PAPLAY_ARGV = (
    "/usr/bin/paplay",
    "--raw",
    "--rate=24000",
    "--format=s16le",
    "--channels=1",
    "--latency-msec=60",
    "--process-time-msec=20",
)
_NETWORK_TICK_SECONDS = 0.02
_PLAYER_WRITE_BYTES = 16 * 1024
_PLAYER_REAP_SECONDS = 0.5
_CONTROL_EVENTS = frozenset(
    {
        "input_audio_buffer.committed",
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "output_audio_buffer.started",
        "output_audio_buffer.stopped",
        "response.created",
        "response.cancelled",
        "response.done",
        "session.started",
        "session.updated",
        "speaking.started",
        "speaking.stopped",
        "turn.created",
        "turn.done",
    }
)


class SessionState(Enum):
    """Externally observable lifecycle without private failure details."""

    NEW = "new"
    CONNECTING = "connecting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class SubmitResult(Enum):
    """Result of a non-blocking microphone submission."""

    ACCEPTED = "accepted"
    GATED = "gated"
    FULL = "full"
    CLOSED = "closed"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class _AudioPacket:
    data: bytes
    captured_at: float


class _BoundedAudioQueue:
    def __init__(self, maximum_bytes: int) -> None:
        self._maximum_bytes = maximum_bytes
        self._bytes = 0
        self._items: deque[_AudioPacket] = deque()
        self._lock = threading.Lock()

    def put(self, packet: _AudioPacket) -> bool:
        with self._lock:
            if self._bytes + len(packet.data) > self._maximum_bytes:
                return False
            self._items.append(packet)
            self._bytes += len(packet.data)
            return True

    def pop(self) -> tuple[_AudioPacket | None, int]:
        with self._lock:
            if not self._items:
                return None, 0
            packet = self._items.popleft()
            self._bytes -= len(packet.data)
            return packet, len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    @property
    def bytes(self) -> int:
        with self._lock:
            return self._bytes


class _AudioPacer:
    """Prevent buffered microphone frames from bursting after startup."""

    def __init__(self) -> None:
        self._next_send_at: float | None = None

    def due(self, now: float) -> bool:
        return self._next_send_at is None or now >= self._next_send_at

    def sent(self, now: float, byte_count: int, *, catching_up: bool = False) -> None:
        # Anchor every frame at its actual send time. A delayed loop therefore
        # never sends several queued capture blocks back-to-back to catch up.
        rate = _MAX_INPUT_CATCH_UP_RATE if catching_up else 1.0
        self._next_send_at = now + byte_count / (_INPUT_BYTES_PER_SECOND * rate)

    def delay(self, now: float) -> float:
        if self._next_send_at is None:
            return 0.0
        return max(0.0, self._next_send_at - now)


class _PcmPlayer:
    """Own one fixed-argv paplay child and its bounded non-blocking stdin."""

    def __init__(
        self,
        maximum_bytes: int,
        *,
        sink: str | None = None,
        volume_percent: int | None = None,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self._maximum_bytes = maximum_bytes
        self._popen = popen
        self._sink = sink
        self._volume = (
            None
            if volume_percent is None
            else _PULSE_VOLUME_NORM * volume_percent // 100
        )
        self._process: subprocess.Popen[bytes] | None = None
        self._stdin: Any = None
        self._pending = bytearray()
        self._finish_when_drained = False
        self._epoch: int | None = None

    def begin(self, epoch: int) -> None:
        self.abort()
        try:
            argv = list(_PAPLAY_ARGV)
            if self._sink is not None:
                argv.append(f"--device={self._sink}")
            if self._volume is not None:
                argv.append(f"--volume={self._volume}")
            process = self._popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                shell=False,
            )
        except Exception as exc:
            raise WebSocketError("paplay could not be started") from exc
        if process.stdin is None:
            _terminate_and_reap(process)
            raise WebSocketError("paplay did not expose stdin")
        try:
            os.set_blocking(process.stdin.fileno(), False)
        except Exception as exc:
            with suppress(Exception):
                process.stdin.close()
            _terminate_and_reap(process)
            raise WebSocketError("paplay stdin could not be configured") from exc
        self._process = process
        self._stdin = process.stdin
        self._finish_when_drained = False
        self._epoch = epoch

    def enqueue(self, value: bytes) -> None:
        if self._process is None or self._stdin is None or self._epoch is None:
            raise WebSocketError("audio arrived outside a speaking epoch")
        if len(self._pending) + len(value) > self._maximum_bytes:
            raise WebSocketError("playback queue exceeded its bound")
        self._pending.extend(value)

    def finish(self, epoch: int) -> None:
        if self._epoch != epoch:
            return
        self._finish_when_drained = True
        self.service()

    def service(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is not None:
            if self._pending:
                raise WebSocketError("paplay exited before consuming output")
            self._reap_finished()
            return
        if self._pending and self._stdin is not None:
            try:
                written = os.write(
                    self._stdin.fileno(), self._pending[:_PLAYER_WRITE_BYTES]
                )
            except BlockingIOError:
                written = 0
            except (BrokenPipeError, OSError) as exc:
                raise WebSocketError("paplay rejected output") from exc
            if written:
                del self._pending[:written]
        if self._finish_when_drained and not self._pending and self._stdin is not None:
            self._stdin.close()
            self._stdin = None
        if process.poll() is not None:
            self._reap_finished()

    def abort(self) -> None:
        process = self._process
        stdin = self._stdin
        self._process = None
        self._stdin = None
        self._pending.clear()
        self._finish_when_drained = False
        self._epoch = None
        if stdin is not None:
            with suppress(Exception):
                stdin.close()
        if process is not None:
            _terminate_and_reap(process)

    @property
    def active(self) -> bool:
        """Return whether queued or child-buffered output can still be audible."""
        return self._process is not None or bool(self._pending)

    def _reap_finished(self) -> None:
        assert self._process is not None
        process = self._process
        self._process = None
        self._stdin = None
        self._epoch = None
        self._finish_when_drained = False
        try:
            process.wait(timeout=_PLAYER_REAP_SECONDS)
        except Exception as exc:
            raise WebSocketError("paplay could not be reaped") from exc


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    """Best-effort bounded child cleanup that never escapes daemon teardown."""
    try:
        running = process.poll() is None
    except Exception:  # noqa: BLE001 - malformed/failed process handle
        running = True
    if running:
        with suppress(Exception):
            process.terminate()
    try:
        process.wait(timeout=_PLAYER_REAP_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    except Exception:  # noqa: BLE001 - still attempt the stronger cleanup
        pass
    else:
        return
    with suppress(Exception):
        process.kill()
    # A second TimeoutExpired/OSError must not prevent session terminal state.
    with suppress(Exception):
        process.wait(timeout=_PLAYER_REAP_SECONDS)


def _verify_pulseaudio_aec(config: RealtimeConfig) -> None:
    """Fail closed unless the configured echo-cancel endpoints are active.

    The voice process starts PulseAudio with module loading disabled, so this
    check deliberately observes startup state instead of trying to mutate it.
    It also runs before the bridge socket is opened: full-duplex audio is never
    submitted when the capture or playback route could bypass AEC.
    """
    if not config.full_duplex:
        return
    source = config.pulse_aec_source
    sink = config.pulse_aec_sink
    if source is None or sink is None:
        raise WebSocketError("PulseAudio echo cancellation is not configured")

    timeout = config.io_timeout_seconds
    default_source = _pactl_output(("get-default-source",), timeout=timeout).strip()
    default_sink = _pactl_output(("get-default-sink",), timeout=timeout).strip()
    if default_source != source or default_sink != sink:
        raise WebSocketError("PulseAudio echo cancellation is not active")

    modules = _pactl_output(("list", "short", "modules"), timeout=timeout)
    if not _has_expected_aec_module(modules, source=source, sink=sink):
        raise WebSocketError("PulseAudio echo cancellation is not active")

    sources = _pactl_output(("list", "short", "sources"), timeout=timeout)
    source_index = _pulse_object_index(sources, source)
    source_outputs = _pactl_output(
        ("--format=json", "list", "source-outputs"), timeout=timeout
    )
    if source_index is None or not _process_capture_uses_source(
        source_outputs, source_index=source_index, process_id=os.getpid()
    ):
        raise WebSocketError("PulseAudio echo cancellation is not active")

    _verify_aec_sink_volume(config)


def _verify_aec_sink_volume(config: RealtimeConfig) -> None:
    """Fail closed if the selected AEC sink is above the canary ceiling."""
    sink = config.pulse_aec_sink
    if sink is None:
        raise WebSocketError("PulseAudio echo cancellation is not configured")
    sink_volume = _pactl_output(
        ("get-sink-volume", sink), timeout=config.io_timeout_seconds
    )
    if not _sink_volume_within_ceiling(
        sink_volume, ceiling=config.aec_test_volume_percent
    ):
        raise WebSocketError("PulseAudio echo cancellation is not active")


def _has_expected_aec_module(modules: str, *, source: str, sink: str) -> bool:
    expected_arguments = {
        f"source_master={_PULSE_SOURCE_MASTER}",
        f"sink_master={_PULSE_SINK_MASTER}",
        f"source_name={source}",
        f"sink_name={sink}",
        "aec_method=webrtc",
        "use_master_format=1",
    }
    for line in modules.splitlines():
        fields = line.split(None, 2)
        if len(fields) != 3 or fields[1] != _PULSE_ECHO_CANCEL_MODULE:
            continue
        try:
            arguments = set(shlex.split(fields[2], comments=False, posix=True))
        except ValueError:
            return False
        if expected_arguments.issubset(arguments):
            return True
    return False


def _pulse_object_index(objects: str, name: str) -> str | None:
    matches = []
    for line in objects.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == name and fields[0].isdigit():
            matches.append(fields[0])
    if len(matches) != 1:
        return None
    return matches[0]


def _process_capture_uses_source(
    source_outputs: str, *, source_index: str, process_id: int
) -> bool:
    try:
        decoded = json.loads(source_outputs)
    except json.JSONDecodeError:
        return False
    if not isinstance(decoded, list):
        return False
    expected_source = int(source_index)
    process_outputs = []
    for candidate in decoded:
        if not isinstance(candidate, dict):
            return False
        properties = candidate.get("properties")
        if not isinstance(properties, dict):
            continue
        if properties.get("application.process.id") == str(process_id):
            process_outputs.append(candidate)
    return bool(process_outputs) and all(
        candidate.get("driver") == _PULSE_NATIVE_DRIVER
        and candidate.get("source") == expected_source
        and candidate.get("corked") is False
        for candidate in process_outputs
    )


def _sink_volume_within_ceiling(value: str, *, ceiling: int) -> bool:
    channels = [int(match) for match in _PULSE_VOLUME_RAW.findall(value)]
    raw_ceiling = _PULSE_VOLUME_NORM * ceiling // 100
    return bool(channels) and all(channel <= raw_ceiling for channel in channels)


def _pactl_output(arguments: tuple[str, ...], *, timeout: float) -> str:
    """Run one fixed-binary, bounded, content-free PulseAudio probe."""
    try:
        result = subprocess.run(
            [*_PACTL_ARGV, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            close_fds=True,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WebSocketError(
            "PulseAudio echo cancellation could not be verified"
        ) from exc
    if result.returncode != 0:
        raise WebSocketError("PulseAudio echo cancellation could not be verified")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WebSocketError(
            "PulseAudio echo cancellation could not be verified"
        ) from exc


_SESSIONS: weakref.WeakSet[RealtimeSession] = weakref.WeakSet()
_SESSIONS_LOCK = threading.Lock()


class RealtimeSession:
    """One fresh bridge session driven by one bounded network thread."""

    def __init__(
        self,
        config: RealtimeConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        connection_factory: Callable[..., WebSocketConnection] = (
            WebSocketConnection.connect
        ),
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        aec_verifier: Callable[[RealtimeConfig], None] | None = None,
        volume_guard: Callable[[RealtimeConfig], None] | None = None,
    ) -> None:
        """Create an unstarted, single-use session."""
        self._config = config
        self._clock = clock
        self._connection_factory = connection_factory
        self._popen = popen
        self._aec_verifier = aec_verifier or _verify_pulseaudio_aec
        # Tests and downstream embedders that supply a complete AEC verifier
        # retain a hermetic guard by default. Production uses the cheaper sink
        # check for every new response after the complete startup preflight.
        self._volume_guard = volume_guard or (
            aec_verifier if aec_verifier is not None else _verify_aec_sink_volume
        )
        self._audio = _BoundedAudioQueue(config.input_queue_bytes)
        self._state = SessionState.NEW
        self._state_lock = threading.Lock()
        self._ready = threading.Event()
        self._terminal = threading.Event()
        self._wake_network = threading.Event()
        self._stop_requested = threading.Event()
        self._interrupt_requested = threading.Event()
        self._interrupt_preserve_session = True
        self._output_active = threading.Event()
        self._suppressed_output_epoch: int | None = None
        self._thread: threading.Thread | None = None
        self._ever_ready = False

    @property
    def state(self) -> SessionState:
        """Return the current lifecycle state."""
        with self._state_lock:
            return self._state

    @property
    def ready(self) -> bool:
        """Return whether protocol v2 negotiation completed."""
        return self._ready.is_set()

    @property
    def output_active(self) -> bool:
        """Return whether the bridge owns the current speaking epoch."""
        return self._output_active.is_set()

    @property
    def failed_before_ready(self) -> bool:
        """Return whether startup ended before a usable session existed."""
        return self._terminal.is_set() and not self._ever_ready

    @property
    def terminal(self) -> bool:
        """Return whether the session thread has completed cleanup."""
        return self._terminal.is_set()

    def start(self) -> None:
        """Start exactly one network thread; a session object is never reusable."""
        with self._state_lock:
            if self._state is not SessionState.NEW:
                raise RuntimeError("realtime session has already been started")
            self._state = SessionState.CONNECTING
        thread = threading.Thread(
            target=self._run,
            name="thirdreality-realtime",
            daemon=True,
        )
        self._thread = thread
        with _SESSIONS_LOCK:
            _SESSIONS.add(self)
        try:
            thread.start()
        except Exception:
            with _SESSIONS_LOCK:
                _SESSIONS.discard(self)
            with self._state_lock:
                self._state = SessionState.FAILED
            self._terminal.set()
            raise

    def submit_audio(self, value: bytes) -> SubmitResult:
        """Queue one microphone frame without blocking the capture thread."""
        if not isinstance(value, bytes) or not value or len(value) % 2:
            return SubmitResult.INVALID
        if len(value) > self._config.max_message_bytes:
            return SubmitResult.INVALID
        # Half-duplex remains the default. Full-duplex construction requires
        # explicit AEC routing, and _run verifies that live PulseAudio topology
        # before opening the bridge socket.
        if self._output_active.is_set() and not self._config.full_duplex:
            return SubmitResult.GATED
        if self.state not in {SessionState.CONNECTING, SessionState.READY}:
            return SubmitResult.CLOSED
        packet = _AudioPacket(value, self._clock())
        if not self._audio.put(packet):
            return SubmitResult.FULL
        self._wake_network.set()
        return SubmitResult.ACCEPTED

    def interrupt(self, *, preserve_session: bool = True) -> None:
        """Flush local output and request bounded provider interruption.

        A session is resumed only after the bridge explicitly confirms remote
        cancellation. Device teardown callers pass ``preserve_session=False``
        so a successful provider cancel cannot outlive the vendor owner.
        """
        if self._terminal.is_set():
            return
        self._output_active.clear()
        with self._state_lock:
            if self._interrupt_requested.is_set():
                self._interrupt_preserve_session = (
                    self._interrupt_preserve_session and preserve_session
                )
            else:
                self._interrupt_preserve_session = preserve_session
            self._interrupt_requested.set()
            if self._state in {SessionState.CONNECTING, SessionState.READY}:
                self._state = SessionState.STOPPING
        self._wake_network.set()

    def stop(self) -> None:
        """Request bounded normal session shutdown."""
        if self._terminal.is_set():
            return
        self._stop_requested.set()
        self._set_stopping()
        self._wake_network.set()

    def join(self, timeout: float) -> bool:
        """Wait a bounded interval and report whether the thread exited."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(0.0, timeout))
        return not thread.is_alive()

    def _set_stopping(self) -> None:
        with self._state_lock:
            if self._state in {SessionState.CONNECTING, SessionState.READY}:
                self._state = SessionState.STOPPING

    def _run(self) -> None:  # noqa: C901 - one bounded protocol state machine
        connection: WebSocketConnection | None = None
        player = _PcmPlayer(
            self._config.output_queue_bytes,
            sink=(self._config.pulse_aec_sink if self._config.full_duplex else None),
            volume_percent=(
                self._config.aec_test_volume_percent
                if self._config.full_duplex
                else None
            ),
            popen=self._popen,
        )
        failed = False
        try:
            started_at = self._clock()
            if self._config.full_duplex:
                self._aec_verifier(self._config)
            connection = self._connection_factory(
                url=self._config.url,
                connect_address=self._config.connect_address,
                token=self._config.token,
                connect_timeout_seconds=self._config.connect_timeout_seconds,
                io_timeout_seconds=self._config.io_timeout_seconds,
                max_message_bytes=self._config.max_message_bytes,
            )
            connection.send_json(realtime_start_message(self._config))
            last_semantic_activity = self._clock()
            handshake_deadline = min(
                started_at + self._config.max_session_seconds,
                last_semantic_activity + self._config.handshake_timeout_seconds,
            )
            self._wait_for_started(connection, handshake_deadline)
            self._ever_ready = True
            self._ready.set()
            with self._state_lock:
                if self._state is SessionState.CONNECTING:
                    self._state = SessionState.READY

            pacer = _AudioPacer()
            last_semantic_activity = self._clock()
            next_ping_at = last_semantic_activity + self._config.ping_interval_seconds
            pending_ping: bytes | None = None
            pong_deadline: float | None = None
            output_epoch: int | None = None
            last_output_epoch = 0
            interrupt_sent = False
            interrupt_deadline: float | None = None

            while True:
                now = self._clock()
                _check_deadlines(
                    now,
                    started_at=started_at,
                    last_activity=last_semantic_activity,
                    max_session_seconds=self._config.max_session_seconds,
                    idle_timeout_seconds=self._config.idle_timeout_seconds,
                    pending_ping=pending_ping,
                    pong_deadline=pong_deadline,
                )

                if self._interrupt_requested.is_set():
                    player.abort()
                    self._audio.clear()
                    self._output_active.clear()
                    if output_epoch is not None:
                        self._suppressed_output_epoch = output_epoch
                    output_epoch = None
                    if not interrupt_sent:
                        connection.send_json({"type": "interrupt"})
                        interrupt_sent = True
                        interrupt_deadline = now + self._config.io_timeout_seconds
                    elif interrupt_deadline is not None and now >= interrupt_deadline:
                        return
                elif self._stop_requested.is_set():
                    player.abort()
                    self._audio.clear()
                    connection.send_json({"type": "stop"})
                    return

                player.service()
                if output_epoch is None and not player.active:
                    self._output_active.clear()
                if not interrupt_sent and pacer.due(now):
                    packet, remaining_packets = self._audio.pop()
                    if packet is not None:
                        connection.send_binary(packet.data)
                        if _pcm_has_signal(packet.data):
                            last_semantic_activity = self._clock()
                        # A bounded 2x catch-up drains only a startup backlog;
                        # once one live frame remains, exact microphone pacing
                        # resumes. This avoids both an unbounded burst and a
                        # permanent handshake-sized delay behind live speech.
                        pacer.sent(
                            now,
                            len(packet.data),
                            catching_up=remaining_packets > 0,
                        )

                if pending_ping is None and now >= next_ping_at:
                    pending_ping = os.urandom(8)
                    connection.send_ping(pending_ping)
                    pong_deadline = now + self._config.pong_timeout_seconds
                    next_ping_at = now + self._config.ping_interval_seconds

                ping_wait = (
                    max(0.0, next_ping_at - now)
                    if pending_ping is None
                    else max(0.0, (pong_deadline or now) - now)
                )
                timeout = min(
                    _NETWORK_TICK_SECONDS,
                    pacer.delay(now) or _NETWORK_TICK_SECONDS,
                    ping_wait,
                    max(
                        0.0,
                        started_at + self._config.max_session_seconds - now,
                    ),
                    max(
                        0.0,
                        last_semantic_activity
                        + self._config.idle_timeout_seconds
                        - now,
                    ),
                )
                if not _socket_readable(connection, timeout):
                    self._wake_network.clear()
                    continue
                message = connection.receive_message()
                if message is None:
                    continue
                if message.kind == "pong":
                    if message.data == pending_ping:
                        pending_ping = None
                        pong_deadline = None
                    continue
                action, output_epoch, last_output_epoch, semantic = (
                    self._handle_message(
                        message,
                        player,
                        output_epoch=output_epoch,
                        last_output_epoch=last_output_epoch,
                    )
                )
                if semantic:
                    last_semantic_activity = self._clock()
                if action == "stop":
                    return
                if interrupt_sent and action == "interrupted":
                    return
                if interrupt_sent and action == "interrupt_resumed":
                    with self._state_lock:
                        preserve_session = self._interrupt_preserve_session
                        if preserve_session:
                            self._interrupt_requested.clear()
                            if self._state is SessionState.STOPPING:
                                self._state = SessionState.READY
                    if not preserve_session:
                        connection.send_json({"type": "stop"})
                        return
                    interrupt_sent = False
                    interrupt_deadline = None
                    self._suppressed_output_epoch = None
                    last_semantic_activity = self._clock()
        except (OSError, TimeoutError, ValueError, WebSocketError):
            failed = not (
                self._stop_requested.is_set() or self._interrupt_requested.is_set()
            )
            if failed:
                _LOGGER.warning("ThirdReality realtime session failed")
        except Exception:  # noqa: BLE001 - never escape the vendor daemon thread
            failed = True
            _LOGGER.warning("ThirdReality realtime session failed", exc_info=False)
        finally:
            try:
                self._output_active.clear()
                self._audio.clear()
                with suppress(Exception):
                    player.abort()
                if connection is not None:
                    with suppress(Exception):
                        connection.send_close()
                    with suppress(Exception):
                        connection.close()
            finally:
                with self._state_lock:
                    self._state = (
                        SessionState.FAILED if failed else SessionState.STOPPED
                    )
                self._terminal.set()
                with _SESSIONS_LOCK:
                    _SESSIONS.discard(self)

    def _wait_for_started(
        self, connection: WebSocketConnection, deadline: float
    ) -> None:
        while True:
            if self._stop_requested.is_set() or self._interrupt_requested.is_set():
                raise WebSocketClosed("realtime startup was cancelled")
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise TimeoutError("realtime start acknowledgement timed out")
            if not _socket_readable(connection, min(_NETWORK_TICK_SECONDS, remaining)):
                continue
            message = connection.receive_message()
            if message is None:
                continue
            if message.kind == "pong":
                continue
            if message.kind != "text":
                raise WebSocketError("realtime start must be acknowledged with JSON")
            value = _json_object(message.data)
            if value.get("type") == "error":
                raise WebSocketError("bridge rejected realtime startup")
            _validate_started(value)
            return

    def _handle_message(  # noqa: C901 - one bounded protocol decoder
        self,
        message: Message,
        player: _PcmPlayer,
        *,
        output_epoch: int | None,
        last_output_epoch: int,
    ) -> tuple[str | None, int | None, int, bool]:
        if message.kind == "binary":
            if output_epoch is None:
                if self._suppressed_output_epoch is not None:
                    return None, None, last_output_epoch, False
                raise WebSocketError("binary output arrived outside a speaking epoch")
            assert isinstance(message.data, bytes)
            player.enqueue(message.data)
            return None, output_epoch, last_output_epoch, True
        if message.kind != "text":
            raise WebSocketError("unexpected WebSocket message kind")
        value = _json_object(message.data)
        message_type = value.get("type")
        if message_type == "control":
            event_type = value.get("event_type")
            if not isinstance(event_type, str) or event_type not in _CONTROL_EVENTS:
                raise WebSocketError("unsupported realtime control event")
            if event_type == "speaking.started":
                epoch = _output_epoch(value)
                if epoch <= last_output_epoch:
                    return None, output_epoch, last_output_epoch, False
                if output_epoch is not None:
                    raise WebSocketError(
                        "overlapping speaking epochs are not supported"
                    )
                # A new monotonic epoch is an unambiguous boundary after any
                # locally suppressed response tail.
                self._suppressed_output_epoch = None
                if self._config.full_duplex:
                    self._volume_guard(self._config)
                player.begin(epoch)
                self._output_active.set()
                return None, epoch, epoch, True
            if event_type == "speaking.stopped":
                epoch = _output_epoch(value)
                if output_epoch is None:
                    if self._suppressed_output_epoch == epoch:
                        self._suppressed_output_epoch = None
                        return None, None, last_output_epoch, True
                    if epoch <= last_output_epoch:
                        return None, None, last_output_epoch, False
                    raise WebSocketError("future speaking stop has no active epoch")
                if epoch != output_epoch:
                    if epoch < output_epoch:
                        return None, output_epoch, last_output_epoch, False
                    raise WebSocketError("speaking stop does not match active epoch")
                player.finish(epoch)
                return None, None, last_output_epoch, True
            if event_type == "input_audio_buffer.speech_started":
                if self._config.full_duplex and (
                    output_epoch is not None or player.active
                ):
                    player.abort()
                    self._output_active.clear()
                    suppressed_epoch = output_epoch or last_output_epoch
                    if suppressed_epoch > 0:
                        self._suppressed_output_epoch = suppressed_epoch
                    return None, None, last_output_epoch, True
            return None, output_epoch, last_output_epoch, True
        if message_type == "pong":
            return None, output_epoch, last_output_epoch, False
        if message_type == "stopped":
            player.abort()
            self._output_active.clear()
            reason = value.get("reason")
            if reason == "interrupt":
                fresh_session_required = value.get("fresh_session_required")
                remote_cancelled = value.get("remote_cancelled")
                if fresh_session_required is True and remote_cancelled is False:
                    self._suppressed_output_epoch = None
                    return "interrupted", None, last_output_epoch, True
                if fresh_session_required is False and remote_cancelled is True:
                    self._suppressed_output_epoch = None
                    return "interrupt_resumed", None, last_output_epoch, True
                raise WebSocketError("bridge returned incompatible interrupt semantics")
            return "stop", None, last_output_epoch, True
        if message_type == "error":
            raise WebSocketError("bridge reported a realtime session error")
        raise WebSocketError("unsupported realtime JSON message")


def shutdown_all_sessions(timeout: float = 2.0) -> None:
    """Best-effort bounded cleanup for process exit."""
    with _SESSIONS_LOCK:
        sessions = list(_SESSIONS)
    deadline = time.monotonic() + max(0.0, timeout)
    for session in sessions:
        session.stop()
    for session in sessions:
        session.join(max(0.0, deadline - time.monotonic()))


def _socket_readable(endpoint: Any, timeout: float) -> bool:
    pending = getattr(endpoint, "pending", None)
    if callable(pending) and pending() > 0:
        return True
    transport = getattr(endpoint, "transport", endpoint)
    transport_pending = getattr(transport, "pending", None)
    if callable(transport_pending) and transport_pending() > 0:
        return True
    readable, _, _ = select.select([transport], [], [], max(0.0, timeout))
    return bool(readable)


def _json_object(value: str | bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WebSocketError("realtime control must be valid JSON") from exc
    if not isinstance(decoded, dict):
        raise WebSocketError("realtime control must be a JSON object")
    return decoded


def _pcm_has_signal(value: bytes) -> bool:
    """Recognize speech-scale input without treating ambient floor as activity."""
    return any(
        abs(sample) >= _INPUT_ACTIVITY_SIGNAL_PEAK
        for (sample,) in struct.iter_unpack("<h", value)
    )


def _validate_started(value: dict[str, Any]) -> None:
    expected = {
        "type": "started",
        "protocol_version": 2,
        "audio_transport": "binary",
        "input_sample_rate": 16_000,
        "input_channels": 1,
        "output_sample_rate": 24_000,
        "output_channels": 1,
    }
    if any(
        value.get(key) != expected_value for key, expected_value in expected.items()
    ):
        raise WebSocketError("bridge returned an incompatible realtime protocol")
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, dict):
        raise WebSocketError("bridge omitted realtime capabilities")
    if capabilities.get("binary_pcm16") is not True:
        raise WebSocketError("bridge does not support binary PCM16")
    if capabilities.get("local_flush") is not True:
        raise WebSocketError("bridge does not support local flush")
    if capabilities.get("remote_cancel") is not False:
        raise WebSocketError("bridge returned incompatible cancel semantics")
    if capabilities.get("same_session_interrupt_ack") is not True:
        raise WebSocketError("bridge omitted same-session interrupt acknowledgement")


def _output_epoch(value: dict[str, Any]) -> int:
    epoch = value.get("output_epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise WebSocketError("output_epoch must be a positive integer")
    return epoch


def _check_deadlines(
    now: float,
    *,
    started_at: float,
    last_activity: float,
    max_session_seconds: float,
    idle_timeout_seconds: float,
    pending_ping: bytes | None,
    pong_deadline: float | None,
) -> None:
    if now - started_at >= max_session_seconds:
        raise WebSocketError("realtime session exceeded its hard limit")
    if now - last_activity >= idle_timeout_seconds:
        raise WebSocketError("realtime session exceeded its idle limit")
    if pending_ping is not None and pong_deadline is not None and now >= pong_deadline:
        raise WebSocketError("realtime pong timed out")
