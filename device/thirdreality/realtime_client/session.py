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
from typing import Any, Protocol

from .config import (
    DEVICE_WEBRTC_TRANSPORT,
    NATIVE_CONVERSATION_MODE,
    RealtimeConfig,
    realtime_start_message,
)
from .playback import (
    PactlSinkVolumeController,
    PulsePlaybackError,
    SinkVolumeController,
)
from .sidecar import (
    ControlMessage,
    PlaybackAudio,
    SidecarError,
    WebRtcSidecarClient,
)
from .websocket import Message, WebSocketClosed, WebSocketConnection, WebSocketError

_LOGGER = logging.getLogger("linux_voice_assistant.realtime")
_INPUT_BYTES_PER_SECOND = 16_000 * 2
_INPUT_ACTIVITY_SIGNAL_PEAK = 256
_LOCAL_BARGE_IN_SIGNAL_PEAK = 1_024
_LOCAL_BARGE_IN_SIGNAL_RMS = 384
_LOCAL_BARGE_IN_FRAMES = 2
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
_DIRECT_HANDSHAKE_TICK_SECONDS = 0.01
_PLAYER_WRITE_BYTES = 24_000 * 2 * 20 // 1_000
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
        "interrupt.fenced",
        "media.quiet",
        "media.started",
    }
)


class SessionState(Enum):
    """Externally observable lifecycle without private failure details."""

    NEW = "new"
    CONNECTING = "connecting"
    READY = "ready"
    INTERRUPTING = "interrupting"
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
    capture_watermark: int


@dataclass(slots=True)
class _DirectPlaybackState:
    """Network-thread-owned generation and playout accounting."""

    active_generation: int | None = None
    newest_generation: int = 0
    retired_generation: int = 0
    expected_sample_index: int | None = None
    interruption_pending: bool = False


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


class _PlayerLike(Protocol):
    """Playback surface shared by rollback and direct-media sessions."""

    def begin(self, epoch: int) -> None: ...

    def resume(self, epoch: int) -> None: ...

    def prepare(self) -> None: ...

    def enqueue(self, value: bytes) -> None: ...

    def finish(self, epoch: int) -> None: ...

    def abort(self) -> None: ...

    def service(self) -> None: ...

    @property
    def active(self) -> bool: ...


class _PcmPlayer:
    """Own one fixed-argv paplay child and its bounded non-blocking stdin."""

    def __init__(
        self,
        maximum_bytes: int,
        *,
        sink: str | None = None,
        volume_percent: int | None = None,
        exact_sink_volume: bool = False,
        volume_controller: SinkVolumeController | None = None,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self._maximum_bytes = maximum_bytes
        self._popen = popen
        self._sink = sink
        self._exact_sink_volume = exact_sink_volume
        self._volume_percent = volume_percent
        self._volume_controller = volume_controller or PactlSinkVolumeController()
        self._volume = (
            None
            if volume_percent is None
            else (
                _PULSE_VOLUME_NORM
                if exact_sink_volume
                else _PULSE_VOLUME_NORM * volume_percent // 100
            )
        )
        self._process: subprocess.Popen[bytes] | None = None
        self._stdin: Any = None
        self._pending = bytearray()
        self._finish_when_drained = False
        self._epoch: int | None = None
        self._volume_prepared = False
        self._reap_pending: list[subprocess.Popen[bytes]] = []

    def prepare(self) -> None:
        """Set and verify the dedicated AEC sink before response media."""
        if not self._exact_sink_volume:
            return
        if self._volume_prepared:
            return
        if self._sink is None or self._volume_percent is None:
            raise WebSocketError("direct playback volume is not configured")
        try:
            self._volume_controller.set_and_verify(
                self._sink,
                self._volume_percent,
            )
        except PulsePlaybackError as exc:
            raise WebSocketError(
                "direct playback volume could not be prepared"
            ) from exc
        self._volume_prepared = True

    def begin(self, epoch: int) -> None:
        self.abort()
        if self._exact_sink_volume and not self._volume_prepared:
            self.prepare()
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
                start_new_session=True,
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

    def resume(self, epoch: int) -> None:
        """Advance a media-fenced epoch without discarding audible tail."""
        if self._process is None or self._stdin is None:
            self.begin(epoch)
            return
        if self._exact_sink_volume and not self._volume_prepared:
            self.prepare()
        self._epoch = epoch
        self._finish_when_drained = False

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
        self._reap_killed()
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
        if process is not None:
            with suppress(Exception):
                process.kill()
            self._reap_pending.append(process)
        if stdin is not None:
            with suppress(Exception):
                stdin.close()

    def close(self, timeout: float = _PLAYER_REAP_SECONDS) -> None:
        """Kill owned playback and reap it within one bounded deadline."""
        self.abort()
        deadline = time.monotonic() + max(0.0, timeout)
        while self._reap_pending and time.monotonic() < deadline:
            self._reap_killed()
            if self._reap_pending:
                time.sleep(0.005)
        self._reap_killed()

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

    def _reap_killed(self) -> None:
        remaining: list[subprocess.Popen[bytes]] = []
        for process in self._reap_pending:
            try:
                if process.poll() is None:
                    remaining.append(process)
                    continue
                process.wait(timeout=0)
            except Exception:  # noqa: BLE001 - SIGKILL already prevents playback.
                if process.poll() is None:
                    remaining.append(process)
        self._reap_pending = remaining


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
    method = config.pulse_aec_method
    if source is None or sink is None or method is None:
        raise WebSocketError("PulseAudio echo cancellation is not configured")

    timeout = config.io_timeout_seconds
    default_source = _pactl_output(("get-default-source",), timeout=timeout).strip()
    default_sink = _pactl_output(("get-default-sink",), timeout=timeout).strip()
    if default_source != source or default_sink != sink:
        raise WebSocketError("PulseAudio echo cancellation is not active")

    modules = _pactl_output(("list", "short", "modules"), timeout=timeout)
    if not _has_expected_aec_module(modules, source=source, sink=sink, method=method):
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
        sink_volume, ceiling=config.aec_sink_volume_ceiling_percent
    ):
        raise WebSocketError("PulseAudio echo cancellation is not active")


def _has_expected_aec_module(
    modules: str, *, source: str, sink: str, method: str
) -> bool:
    expected_arguments = {
        f"source_master={_PULSE_SOURCE_MASTER}",
        f"sink_master={_PULSE_SINK_MASTER}",
        f"source_name={source}",
        f"sink_name={sink}",
        f"aec_method={method}",
        "use_master_format=1",
    }
    for line in modules.splitlines():
        fields = line.split(None, 2)
        if len(fields) != 3 or fields[1] != _PULSE_ECHO_CANCEL_MODULE:
            continue
        try:
            arguments = shlex.split(fields[2], comments=False, posix=True)
        except ValueError:
            return False
        if (
            len(arguments) == len(expected_arguments)
            and set(arguments) == expected_arguments
        ):
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
_PREWARM_LOCK = threading.Lock()
_PREWARMED_SIDECAR: WebRtcSidecarClient | None = None
_SHUTTING_DOWN = False


def prewarm_device_webrtc() -> bool:
    """Keep one isolated peer process warm between direct voice sessions."""
    global _PREWARMED_SIDECAR  # noqa: PLW0603
    with _PREWARM_LOCK:
        if _SHUTTING_DOWN:
            return False
        current = _PREWARMED_SIDECAR
        if current is not None:
            if not current.closed and current.process.poll() is None:
                return True
            with suppress(Exception):
                current.close()
            _PREWARMED_SIDECAR = None
        try:
            _PREWARMED_SIDECAR = WebRtcSidecarClient.launch()
        except Exception:  # noqa: BLE001 - optional prewarm fails closed on wake
            _LOGGER.warning("ThirdReality WebRTC prewarm failed", exc_info=False)
            return False
        return True


def _take_prewarmed_sidecar() -> WebRtcSidecarClient:
    """Transfer the warm peer to one session, cold-launching if unavailable."""
    global _PREWARMED_SIDECAR  # noqa: PLW0603
    with _PREWARM_LOCK:
        if _SHUTTING_DOWN:
            raise SidecarError("device WebRTC process is shutting down")
        sidecar = _PREWARMED_SIDECAR
        _PREWARMED_SIDECAR = None
    if sidecar is not None:
        if not sidecar.closed and sidecar.process.poll() is None:
            return sidecar
        with suppress(Exception):
            sidecar.close()
    return WebRtcSidecarClient.launch()


def _close_prewarmed_sidecar(*, timeout: float = 1.0) -> None:
    """Release the idle isolated peer during process shutdown."""
    global _PREWARMED_SIDECAR, _SHUTTING_DOWN  # noqa: PLW0603
    with _PREWARM_LOCK:
        _SHUTTING_DOWN = True
        sidecar = _PREWARMED_SIDECAR
        _PREWARMED_SIDECAR = None
    if sidecar is not None:
        with suppress(Exception):
            sidecar.close(timeout=max(0.0, timeout))


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
        sidecar_factory: Callable[[], WebRtcSidecarClient] | None = None,
        direct_player_factory: Callable[[int, str], _PlayerLike] | None = None,
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
        self._uses_global_sidecar = sidecar_factory is None
        self._sidecar_factory = sidecar_factory or _take_prewarmed_sidecar
        self._direct_player_factory = direct_player_factory or (
            lambda maximum_bytes, sink: _PcmPlayer(
                maximum_bytes,
                sink=sink,
                volume_percent=self._config.playback_volume_percent,
                exact_sink_volume=True,
                popen=self._popen,
            )
        )
        self._audio = _BoundedAudioQueue(config.input_queue_bytes)
        self._accepted_capture_watermark = 0
        self._sent_capture_watermark = 0
        self._state = SessionState.NEW
        self._audio_send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._ready = threading.Event()
        self._terminal = threading.Event()
        self._wake_network = threading.Event()
        self._stop_requested = threading.Event()
        self._interrupt_requested = threading.Event()
        self._interrupt_preserve_session = True
        self._output_active = threading.Event()
        self._local_output_epoch: int | None = None
        self._local_barge_in_requested_epoch: int | None = None
        self._local_barge_in_requested_watermark: int | None = None
        self._local_barge_in_frames = 0
        self._local_barge_in_lock = threading.Lock()
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
        captured_at = self._clock()
        with self._state_lock:
            if self._state not in {
                SessionState.CONNECTING,
                SessionState.READY,
                SessionState.INTERRUPTING,
            }:
                return SubmitResult.CLOSED
            if self._output_active.is_set() and not self._config.full_duplex:
                return SubmitResult.GATED
            capture_watermark = self._accepted_capture_watermark + 1
            packet = _AudioPacket(value, captured_at, capture_watermark)
            if not self._audio.put(packet):
                if (
                    self._config.media_transport == DEVICE_WEBRTC_TRANSPORT
                    and self._detect_local_barge_in(
                        value,
                        capture_watermark=None,
                    )
                ):
                    # The causal speech frame was not admitted, so a same-peer
                    # fence could never prove that the provider consumed it.
                    # Kill output through the normal direct-session teardown
                    # path instead of fabricating a capture watermark.
                    self._interrupt_preserve_session = False
                    self._interrupt_requested.set()
                    self._state = SessionState.STOPPING
                    self._audio.clear()
                    self._reset_local_barge_in_detection()
                    self._wake_network.set()
                return SubmitResult.FULL
            self._accepted_capture_watermark = capture_watermark
            # Keep admission and detection on the same side of an interrupt or
            # stop transition. The transition owns the same state lock and can
            # therefore clear every packet and pending detector result admitted
            # before it, while later submissions observe STOPPING.
            self._detect_local_barge_in(
                value,
                capture_watermark=capture_watermark,
            )
        self._wake_network.set()
        return SubmitResult.ACCEPTED

    def _detect_local_barge_in(
        self,
        value: bytes,
        *,
        capture_watermark: int | None,
    ) -> bool:
        """Detect bounded AEC-filtered speech; report an unfenceable trigger."""
        if not self._config.full_duplex:
            return False
        with self._local_barge_in_lock:
            output_epoch = self._local_output_epoch
            if output_epoch is None:
                self._local_barge_in_requested_watermark = None
                self._local_barge_in_frames = 0
                return False
            if self._local_barge_in_requested_epoch is not None:
                return False
            if _pcm_has_local_barge_in_signal(value):
                self._local_barge_in_frames += 1
            else:
                self._local_barge_in_frames = 0
            if self._local_barge_in_frames < _LOCAL_BARGE_IN_FRAMES:
                return False
            self._local_barge_in_frames = 0
            if capture_watermark is None:
                return True
            self._local_barge_in_requested_epoch = output_epoch
            self._local_barge_in_requested_watermark = capture_watermark
            return False

    def _set_local_output_epoch(self, output_epoch: int | None) -> None:
        """Publish one network-thread-owned playback generation to capture."""
        with self._local_barge_in_lock:
            self._local_output_epoch = output_epoch
            self._local_barge_in_requested_epoch = None
            self._local_barge_in_requested_watermark = None
            self._local_barge_in_frames = 0
            if output_epoch is None:
                self._output_active.clear()
            else:
                self._output_active.set()

    def _reset_local_barge_in_detection(self) -> None:
        """Discard detector state without changing network-owned playback."""
        with self._local_barge_in_lock:
            self._local_barge_in_requested_epoch = None
            self._local_barge_in_requested_watermark = None
            self._local_barge_in_frames = 0

    def _flush_local_barge_in(
        self,
        player: _PlayerLike,
        *,
        output_epoch: int | None,
        last_output_epoch: int,
    ) -> tuple[int | None, int | None]:
        """Apply a capture request and return output plus its send watermark."""
        with self._local_barge_in_lock:
            requested_epoch = self._local_barge_in_requested_epoch
            if requested_epoch is None:
                # Network polls run more frequently than the fixed recorder
                # callback. No request is not a detector boundary: preserve a
                # qualifying partial count until the next microphone frame.
                return output_epoch, None
            requested_watermark = self._local_barge_in_requested_watermark
            self._local_barge_in_requested_epoch = None
            self._local_barge_in_requested_watermark = None
            self._local_barge_in_frames = 0
            matches_current_output = requested_epoch == output_epoch or (
                output_epoch is None
                and requested_epoch == last_output_epoch
                and player.active
            )
            if (
                not matches_current_output
                or requested_epoch != self._local_output_epoch
            ):
                return output_epoch, None
            # Disable capture-side detection before the potentially blocking
            # player reap. Only this network-thread path mutates playback state.
            self._local_output_epoch = None
            self._output_active.clear()
        player.abort()
        assert requested_epoch is not None
        assert requested_watermark is not None
        self._suppressed_output_epoch = requested_epoch
        return None, requested_watermark

    def interrupt(self, *, preserve_session: bool = True) -> None:
        """Flush local output and request bounded provider interruption.

        Explicit direct-session interruption always closes the peer because it
        has no AEC-qualified speech evidence. Automatic local barge-in uses the
        separate media fence in the direct network loop. Bridge-PCM sessions
        retain their negotiated same-session interruption behavior.
        """
        if self._terminal.is_set():
            return
        with self._audio_send_lock, self._state_lock:
            if self._interrupt_requested.is_set():
                self._interrupt_preserve_session = (
                    self._interrupt_preserve_session and preserve_session
                )
            else:
                self._interrupt_preserve_session = preserve_session
            self._interrupt_requested.set()
            if (
                not self._interrupt_preserve_session
                or self._config.media_transport == DEVICE_WEBRTC_TRANSPORT
            ):
                self._state = SessionState.STOPPING
            elif self._state in {SessionState.CONNECTING, SessionState.READY}:
                self._state = (
                    SessionState.INTERRUPTING
                    if self._config.media_transport == DEVICE_WEBRTC_TRANSPORT
                    else SessionState.STOPPING
                )
            self._audio.clear()
            self._reset_local_barge_in_detection()
        self._wake_network.set()

    def stop(self) -> None:
        """Request bounded normal session shutdown."""
        if self._terminal.is_set():
            return
        with self._audio_send_lock, self._state_lock:
            self._stop_requested.set()
            if self._state in {
                SessionState.CONNECTING,
                SessionState.READY,
                SessionState.INTERRUPTING,
            }:
                self._state = SessionState.STOPPING
            self._audio.clear()
            self._reset_local_barge_in_detection()
        self._wake_network.set()

    def join(self, timeout: float) -> bool:
        """Wait a bounded interval and report whether the thread exited."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(0.0, timeout))
        return not thread.is_alive()

    def _run(self) -> None:
        """Select the explicitly configured media transport."""
        if self._config.media_transport == DEVICE_WEBRTC_TRANSPORT:
            self._run_device_webrtc()
            return
        self._run_bridge_pcm()

    def _send_bridge_audio(
        self,
        connection: WebSocketConnection,
    ) -> tuple[_AudioPacket | None, int]:
        """Send one queued bridge frame on the explicit-stop boundary."""
        # A distinct send boundary lets the recorder keep admitting capture
        # while a bounded WebSocket write blocks. Stop/interrupt acquire this
        # lock before changing state, so a transition that returns has either
        # waited for this reserved send or prevented its dequeue.
        with self._audio_send_lock:
            with self._state_lock:
                if (
                    self._stop_requested.is_set()
                    or self._interrupt_requested.is_set()
                    or self._state
                    not in {
                        SessionState.CONNECTING,
                        SessionState.READY,
                        SessionState.INTERRUPTING,
                    }
                ):
                    return None, 0
                packet, remaining_packets = self._audio.pop()
                if packet is None:
                    return None, 0
            connection.send_binary(packet.data)
            self._sent_capture_watermark = packet.capture_watermark
            return packet, remaining_packets

    def _run_bridge_pcm(  # noqa: C901
        self,
    ) -> None:
        connection: WebSocketConnection | None = None
        player = _PcmPlayer(
            self._config.output_queue_bytes,
            sink=(self._config.pulse_aec_sink if self._config.full_duplex else None),
            volume_percent=(
                self._config.playback_volume_percent
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
                    self._set_local_output_epoch(None)
                    player.abort()
                    self._audio.clear()
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
                    self._set_local_output_epoch(None)
                    player.abort()
                    self._audio.clear()
                    connection.send_json({"type": "stop"})
                    return
                else:
                    output_epoch, _ = self._flush_local_barge_in(
                        player,
                        output_epoch=output_epoch,
                        last_output_epoch=last_output_epoch,
                    )

                player.service()
                if output_epoch is None and not player.active:
                    self._set_local_output_epoch(None)
                if not interrupt_sent and pacer.due(now):
                    packet, remaining_packets = self._send_bridge_audio(connection)
                    if packet is not None:
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
                    self._set_local_output_epoch(None)
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
                self._set_local_output_epoch(None)
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

    def _run_device_webrtc(  # noqa: C901
        self,
    ) -> None:
        """Terminate WebRTC on the device and keep the bridge signaling-only."""
        connection: WebSocketConnection | None = None
        sidecar: WebRtcSidecarClient | None = None
        player: _PlayerLike | None = None
        failed = False
        capture_ages_ms: deque[float] = deque(maxlen=256)
        try:
            started_at = self._clock()
            self._aec_verifier(self._config)
            sink = self._config.pulse_aec_sink
            if sink is None:
                raise WebSocketError(  # noqa: TRY301
                    "direct WebRTC playback has no AEC sink"
                )
            player = self._direct_player_factory(
                self._config.output_queue_bytes,
                sink,
            )
            # Perform the bounded sink mutation before negotiation. The live
            # media loop must never block on pactl while it owns VAD, interrupt,
            # IPC draining, and immediate paplay termination.
            player.prepare()
            state = _DirectPlaybackState()
            sidecar = self._sidecar_factory()
            handshake_deadline = min(
                started_at + self._config.max_session_seconds,
                started_at + self._config.handshake_timeout_seconds,
            )

            sidecar.request_offer()
            offer_sdp = self._wait_for_direct_offer(sidecar, handshake_deadline)
            connection = self._connection_factory(
                url=self._config.url,
                connect_address=self._config.connect_address,
                token=self._config.token,
                connect_timeout_seconds=self._config.connect_timeout_seconds,
                io_timeout_seconds=self._config.io_timeout_seconds,
                max_message_bytes=self._config.max_message_bytes,
            )
            connection.send_json(
                realtime_start_message(self._config, webrtc_sdp=offer_sdp)
            )
            answer_sdp = self._wait_for_direct_answer(
                connection,
                sidecar,
                handshake_deadline,
            )
            sidecar.set_answer(answer_sdp)

            pacer = _AudioPacer()
            sample_index = 0
            ready_states: set[str] = set()
            required_states = {"answer.applied", "connected", "data.ready"}
            while not required_states.issubset(ready_states):
                self._raise_if_direct_startup_cancelled(handshake_deadline)
                now = self._clock()
                sample_index, _ = self._send_direct_audio(
                    sidecar,
                    pacer,
                    sample_index=sample_index,
                    now=now,
                    capture_ages_ms=capture_ages_ms,
                )
                controls, _ = self._drain_direct_sidecar(
                    sidecar,
                    player,
                    state,
                )
                for control in controls:
                    if control.type in required_states:
                        ready_states.add(control.type)
                    else:
                        raise SidecarError(  # noqa: TRY301
                            "sidecar emitted an unexpected handshake event"
                        )
                self._reject_direct_bridge_message_if_ready(connection)
                self._wait_direct_tick(pacer, now, handshake_deadline)

            connection.send_json({"type": "transport_ready", "protocol_version": 3})
            while True:
                self._raise_if_direct_startup_cancelled(handshake_deadline)
                now = self._clock()
                sample_index, _ = self._send_direct_audio(
                    sidecar,
                    pacer,
                    sample_index=sample_index,
                    now=now,
                    capture_ages_ms=capture_ages_ms,
                )
                controls, _ = self._drain_direct_sidecar(sidecar, player, state)
                if controls:
                    raise SidecarError(  # noqa: TRY301
                        "sidecar emitted an unexpected startup event"
                    )
                if _socket_readable(connection, 0):
                    message = connection.receive_message()
                    if message is None:
                        continue
                    if message.kind == "pong":
                        continue
                    if message.kind != "text":
                        raise WebSocketError(  # noqa: TRY301
                            "direct WebRTC startup received non-JSON data"
                        )
                    value = _json_object(message.data)
                    if value.get("type") == "error":
                        raise WebSocketError(  # noqa: TRY301
                            "bridge rejected direct WebRTC startup"
                        )
                    _validate_direct_started(value)
                    break
                self._wait_direct_tick(pacer, now, handshake_deadline)

            self._ever_ready = True
            self._ready.set()
            with self._state_lock:
                if self._state is SessionState.CONNECTING:
                    self._state = SessionState.READY

            last_semantic_activity = self._clock()
            next_ping_at = last_semantic_activity + self._config.ping_interval_seconds
            pending_ping: bytes | None = None
            pong_deadline: float | None = None
            pending_interrupt_watermark: int | None = None

            while True:
                now = self._clock()
                player.service()
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
                    self._set_local_output_epoch(None)
                    if state.active_generation is not None:
                        state.retired_generation = max(
                            state.retired_generation,
                            state.active_generation,
                        )
                    self._audio.clear()
                    sidecar.stop()
                    connection.send_json({"type": "stop"})
                    return
                if self._stop_requested.is_set():
                    player.abort()
                    self._set_local_output_epoch(None)
                    self._audio.clear()
                    sidecar.stop()
                    connection.send_json({"type": "stop"})
                    return
                previous_generation = state.active_generation
                active_generation, requested_watermark = self._flush_local_barge_in(
                    player,
                    output_epoch=previous_generation,
                    last_output_epoch=state.newest_generation,
                )
                if previous_generation is not None and active_generation is None:
                    state.retired_generation = max(
                        state.retired_generation,
                        previous_generation,
                    )
                    state.interruption_pending = True
                    state.active_generation = None
                    assert requested_watermark is not None
                    pending_interrupt_watermark = requested_watermark
                    with self._state_lock:
                        if self._state is SessionState.READY:
                            self._state = SessionState.INTERRUPTING
                    last_semantic_activity = now

                if (
                    pending_interrupt_watermark is not None
                    and self._sent_capture_watermark >= pending_interrupt_watermark
                ):
                    sidecar.interrupt_response()
                    pending_interrupt_watermark = None
                sample_index, input_semantic = self._send_direct_audio(
                    sidecar,
                    pacer,
                    sample_index=sample_index,
                    now=now,
                    capture_ages_ms=capture_ages_ms,
                )
                if (
                    pending_interrupt_watermark is not None
                    and self._sent_capture_watermark >= pending_interrupt_watermark
                ):
                    # The sidecar uses one ordered SOCK_SEQPACKET channel for
                    # capture and controls. Fence only after the exact second
                    # qualifying AEC frame crosses that channel. Playback was
                    # already killed above while the backlog stayed paced.
                    sidecar.interrupt_response()
                    pending_interrupt_watermark = None
                controls, output_semantic = self._drain_direct_sidecar(
                    sidecar,
                    player,
                    state,
                )
                if controls:
                    raise SidecarError(  # noqa: TRY301
                        "sidecar emitted an unexpected runtime event"
                    )
                if input_semantic or output_semantic:
                    last_semantic_activity = self._clock()

                if pending_ping is None and now >= next_ping_at:
                    pending_ping = os.urandom(8)
                    connection.send_ping(pending_ping)
                    pong_deadline = now + self._config.pong_timeout_seconds
                    next_ping_at = now + self._config.ping_interval_seconds

                if _socket_readable(connection, 0):
                    message = connection.receive_message()
                    if message is None:
                        continue
                    if message.kind == "pong":
                        if message.data == pending_ping:
                            pending_ping = None
                            pong_deadline = None
                        continue
                    if message.kind != "text":
                        raise WebSocketError(  # noqa: TRY301
                            "direct WebRTC bridge carried unexpected media"
                        )
                    value = _json_object(message.data)
                    message_type = value.get("type")
                    if message_type == "pong":
                        continue
                    if message_type == "stopped":
                        return
                    if message_type == "error":
                        raise WebSocketError(  # noqa: TRY301
                            "direct WebRTC bridge reported an error"
                        )
                    raise WebSocketError(  # noqa: TRY301
                        "unsupported direct WebRTC bridge control"
                    )

                ping_wait = (
                    max(0.0, next_ping_at - now)
                    if pending_ping is None
                    else max(0.0, (pong_deadline or now) - now)
                )
                timeout = min(
                    _NETWORK_TICK_SECONDS,
                    pacer.delay(now) or _NETWORK_TICK_SECONDS,
                    ping_wait,
                    max(0.0, started_at + self._config.max_session_seconds - now),
                    max(
                        0.0,
                        last_semantic_activity
                        + self._config.idle_timeout_seconds
                        - now,
                    ),
                )
                self._wake_network.wait(timeout)
                self._wake_network.clear()
        except (
            OSError,
            PulsePlaybackError,
            SidecarError,
            TimeoutError,
            ValueError,
            WebSocketError,
        ):
            failed = not (
                self._stop_requested.is_set() or self._interrupt_requested.is_set()
            )
            if failed:
                _LOGGER.warning("ThirdReality direct WebRTC session failed")
        except Exception:  # noqa: BLE001 - never escape the vendor daemon thread
            failed = True
            _LOGGER.warning(
                "ThirdReality direct WebRTC session failed",
                exc_info=False,
            )
        finally:
            try:
                self._set_local_output_epoch(None)
                self._audio.clear()
                if player is not None:
                    with suppress(Exception):
                        player.abort()
                    close_player = getattr(player, "close", None)
                    if callable(close_player):
                        with suppress(Exception):
                            close_player(timeout=0.5)
                if sidecar is not None:
                    with suppress(Exception):
                        sidecar.stop()
                    with suppress(Exception):
                        sidecar.close()
                if connection is not None:
                    with suppress(Exception):
                        connection.send_close()
                    with suppress(Exception):
                        connection.close()
            finally:
                if capture_ages_ms:
                    ordered_ages = sorted(capture_ages_ms)
                    p95_index = max(0, int(len(ordered_ages) * 0.95) - 1)
                    _LOGGER.info(
                        "ThirdReality direct capture age: p95_ms=%.1f max_ms=%.1f",
                        ordered_ages[p95_index],
                        ordered_ages[-1],
                    )
                with self._state_lock:
                    self._state = (
                        SessionState.FAILED if failed else SessionState.STOPPED
                    )
                self._terminal.set()
                with _SESSIONS_LOCK:
                    _SESSIONS.discard(self)
                if self._uses_global_sidecar:
                    prewarm_device_webrtc()

    def _wait_for_direct_offer(
        self,
        sidecar: WebRtcSidecarClient,
        deadline: float,
    ) -> str:
        """Wait for exactly one content-free sidecar offer."""
        while True:
            self._raise_if_direct_startup_cancelled(deadline)
            if _socket_readable(sidecar, 0):
                for message in sidecar.drain_messages():
                    if (
                        isinstance(message, ControlMessage)
                        and message.type == "offer"
                        and isinstance(message.values.get("sdp"), str)
                    ):
                        return str(message.values["sdp"])
                    if isinstance(message, ControlMessage) and message.type == "error":
                        raise SidecarError("sidecar could not create an offer")
                    raise SidecarError("sidecar emitted an unexpected offer event")
            self._wake_network.wait(_DIRECT_HANDSHAKE_TICK_SECONDS)
            self._wake_network.clear()

    def _wait_for_direct_answer(
        self,
        connection: WebSocketConnection,
        sidecar: WebRtcSidecarClient,
        deadline: float,
    ) -> str:
        """Wait for one strict bridge answer while monitoring the sidecar."""
        while True:
            self._raise_if_direct_startup_cancelled(deadline)
            if _socket_readable(sidecar, 0):
                messages = sidecar.drain_messages()
                if messages:
                    raise SidecarError("sidecar failed while awaiting SDP answer")
            if _socket_readable(connection, 0):
                message = connection.receive_message()
                if message is None:
                    continue
                if message.kind == "pong":
                    continue
                if message.kind != "text":
                    raise WebSocketError("direct WebRTC answer must be JSON")
                value = _json_object(message.data)
                if value.get("type") == "error":
                    raise WebSocketError("bridge rejected direct WebRTC startup")
                return _direct_answer_sdp(value)
            self._wake_network.wait(_DIRECT_HANDSHAKE_TICK_SECONDS)
            self._wake_network.clear()

    def _raise_if_direct_startup_cancelled(self, deadline: float) -> None:
        """Apply the single startup deadline and stop boundary."""
        if self._stop_requested.is_set() or self._interrupt_requested.is_set():
            raise WebSocketClosed("direct WebRTC startup was cancelled")
        if self._clock() >= deadline:
            raise TimeoutError("direct WebRTC startup timed out")

    def _wait_direct_tick(
        self,
        pacer: _AudioPacer,
        now: float,
        deadline: float,
    ) -> None:
        """Yield a bounded handshake tick without delaying queued microphone PCM."""
        timeout = min(
            _DIRECT_HANDSHAKE_TICK_SECONDS,
            pacer.delay(now) or _DIRECT_HANDSHAKE_TICK_SECONDS,
            max(0.0, deadline - now),
        )
        self._wake_network.wait(timeout)
        self._wake_network.clear()

    def _reject_direct_bridge_message_if_ready(
        self,
        connection: WebSocketConnection,
    ) -> None:
        """Fail closed if the bridge speaks before transport readiness."""
        if not _socket_readable(connection, 0):
            return
        message = connection.receive_message()
        if message is None:
            return
        if message.kind == "pong":
            return
        if message.kind == "text" and _json_object(message.data).get("type") == "error":
            raise WebSocketError("bridge rejected direct WebRTC transport")
        raise WebSocketError("bridge sent an unexpected direct WebRTC message")

    def _send_direct_audio(
        self,
        sidecar: WebRtcSidecarClient,
        pacer: _AudioPacer,
        *,
        sample_index: int,
        now: float,
        capture_ages_ms: deque[float],
    ) -> tuple[int, bool]:
        """Send at most one timestamped frame, preserving bounded startup catch-up."""
        if not pacer.due(now):
            return sample_index, False
        # Linearize dequeue and the non-blocking IPC send with explicit stop
        # and interrupt. A transition that returns has either cleared this
        # packet before dequeue or waited until its send completed.
        with self._state_lock:
            if (
                self._stop_requested.is_set()
                or self._interrupt_requested.is_set()
                or self._state
                not in {
                    SessionState.CONNECTING,
                    SessionState.READY,
                    SessionState.INTERRUPTING,
                }
            ):
                return sample_index, False
            packet, remaining_packets = self._audio.pop()
            if packet is None:
                return sample_index, False
            sidecar.send_audio(
                packet.data,
                sample_index=sample_index,
                capture_monotonic_ns=max(
                    0,
                    int(packet.captured_at * 1_000_000_000),
                ),
            )
            self._sent_capture_watermark = packet.capture_watermark
        capture_ages_ms.append(max(0.0, (now - packet.captured_at) * 1_000))
        sample_index += len(packet.data) // 2
        pacer.sent(
            now,
            len(packet.data),
            catching_up=remaining_packets > 0,
        )
        return sample_index, _pcm_has_signal(packet.data)

    def _drain_direct_sidecar(
        self,
        sidecar: WebRtcSidecarClient,
        player: _PlayerLike,
        state: _DirectPlaybackState,
    ) -> tuple[list[ControlMessage], bool]:
        """Consume bounded IPC, returning only non-media state messages."""
        if not _socket_readable(sidecar, 0):
            return [], False
        controls: list[ControlMessage] = []
        semantic = False
        for message in sidecar.drain_messages(maximum=8):
            if isinstance(message, PlaybackAudio):
                self._handle_direct_playback(message, player, state)
                semantic = True
                continue
            if message.type == "error":
                raise SidecarError("device WebRTC sidecar reported an error")
            if message.type == "lifecycle":
                if self._handle_direct_lifecycle(message, sidecar, player, state):
                    semantic = True
                continue
            controls.append(message)
        return controls, semantic

    def _handle_direct_playback(
        self,
        message: PlaybackAudio,
        player: _PlayerLike,
        state: _DirectPlaybackState,
    ) -> None:
        """Render contiguous PCM only inside a sidecar-proven media epoch."""
        samples = len(message.pcm) // 2
        if message.generation < 1:
            raise SidecarError("playback generation must be positive")
        if (
            state.expected_sample_index is not None
            and message.sample_index != state.expected_sample_index
        ):
            raise SidecarError("playback sample sequence is not contiguous")
        state.expected_sample_index = message.sample_index + samples
        if state.interruption_pending:
            return
        if message.generation <= state.retired_generation:
            return
        if message.generation < state.newest_generation:
            return
        if message.generation > state.newest_generation:
            raise SidecarError("playback arrived before its media boundary")
        if state.active_generation != message.generation:
            raise SidecarError("playback arrived outside its active media epoch")
        player.enqueue(message.pcm)

    def _handle_direct_lifecycle(
        self,
        message: ControlMessage,
        sidecar: WebRtcSidecarClient,
        player: _PlayerLike,
        state: _DirectPlaybackState,
    ) -> bool:
        """Apply sanitized provider lifecycle without transcript exposure."""
        event_type = message.values.get("event_type")
        generation = message.values.get("generation")
        if not isinstance(event_type, str) or not isinstance(generation, int):
            raise SidecarError("sidecar lifecycle metadata is invalid")
        if generation < 0:
            raise SidecarError("sidecar lifecycle generation is invalid")
        if event_type == "media.started":
            if generation <= 0:
                raise SidecarError("media generation must be positive")
            if state.interruption_pending:
                # The parent kills playback before the paced capture backlog
                # reaches the qualifying-frame watermark. Until the child sees
                # the ordered interrupt token it can legitimately observe a
                # normal 120 ms gap and open another stale generation. Retire
                # every such generation locally; its playback packets are then
                # dropped by the generation gate. The child fence remains the
                # sole authority that can later return this session to READY.
                if generation <= state.retired_generation:
                    return False
                if generation <= state.newest_generation:
                    raise SidecarError("muted media generation did not advance")
                if state.active_generation is not None:
                    raise SidecarError("muted media generation overlapped output")
                state.newest_generation = generation
                state.retired_generation = generation
                return True
            if generation <= state.retired_generation:
                return False
            if generation <= state.newest_generation:
                raise SidecarError("media generation did not advance")
            if state.active_generation is not None:
                raise SidecarError("media generation overlapped its predecessor")
            if player.active:
                resume = getattr(player, "resume", None)
                if not callable(resume):
                    raise SidecarError("playback cannot resume a media epoch")
                resume(generation)
            else:
                player.begin(generation)
            self._set_local_output_epoch(generation)
            state.newest_generation = generation
            state.active_generation = generation
            self._suppressed_output_epoch = None
            return True

        if event_type == "media.quiet":
            if generation == state.active_generation:
                self._set_local_output_epoch(None)
                state.retired_generation = max(state.retired_generation, generation)
                state.active_generation = None
            return True

        if event_type == "interrupt.fenced":
            state.interruption_pending = False
            with self._state_lock:
                if self._state is SessionState.INTERRUPTING:
                    self._state = SessionState.READY
            return True

        if event_type == "error":
            raise SidecarError("realtime provider reported an error")
        return event_type in _CONTROL_EVENTS or event_type.startswith("session.")

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
                # Retire any draining predecessor and its pending detector
                # result before publishing this fresh monotonic generation.
                self._set_local_output_epoch(None)
                player.begin(epoch)
                self._set_local_output_epoch(epoch)
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
                    self._set_local_output_epoch(None)
                    player.abort()
                    suppressed_epoch = output_epoch or last_output_epoch
                    if suppressed_epoch > 0:
                        self._suppressed_output_epoch = suppressed_epoch
                    return None, None, last_output_epoch, True
            return None, output_epoch, last_output_epoch, True
        if message_type == "pong":
            return None, output_epoch, last_output_epoch, False
        if message_type == "stopped":
            self._set_local_output_epoch(None)
            player.abort()
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
                if (
                    fresh_session_required is False
                    and remote_cancelled is False
                    and value.get("continuation_safe") is True
                ):
                    # Broker-managed realtime invalidates the bridge-owned
                    # executor/output generation locally. The voice transport
                    # may therefore remain open even when the provider did not
                    # confirm cancellation of its side-effect-free frontend.
                    self._suppressed_output_epoch = None
                    return "interrupt_resumed", None, last_output_epoch, True
                raise WebSocketError("bridge returned incompatible interrupt semantics")
            return "stop", None, last_output_epoch, True
        if message_type == "error":
            raise WebSocketError("bridge reported a realtime session error")
        raise WebSocketError("unsupported realtime JSON message")


def shutdown_all_sessions(timeout: float = 2.0) -> None:
    """Best-effort bounded cleanup for process exit."""
    global _PREWARMED_SIDECAR, _SHUTTING_DOWN  # noqa: PLW0603
    deadline = time.monotonic() + max(0.0, timeout)
    with _PREWARM_LOCK:
        _SHUTTING_DOWN = True
        prewarmed = _PREWARMED_SIDECAR
        _PREWARMED_SIDECAR = None
    with _SESSIONS_LOCK:
        sessions = list(_SESSIONS)
    # Signal every active media owner before spending any shared deadline on
    # the idle prewarm. This guarantees local playback kill/flush begins even
    # when an isolated child is wedged during process exit.
    for session in sessions:
        session.stop()
    if prewarmed is not None:
        with suppress(Exception):
            prewarmed.close(timeout=max(0.0, deadline - time.monotonic()))
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


def _pcm_has_local_barge_in_signal(value: bytes) -> bool:
    """Require both a speech-scale peak and sustained frame energy."""
    sample_count = len(value) // 2
    if sample_count == 0:
        return False
    peak = 0
    energy = 0
    for (sample,) in struct.iter_unpack("<h", value):
        magnitude = abs(sample)
        peak = max(peak, magnitude)
        energy += sample * sample
    return peak >= _LOCAL_BARGE_IN_SIGNAL_PEAK and energy >= (
        _LOCAL_BARGE_IN_SIGNAL_RMS**2 * sample_count
    )


def _validate_started(value: dict[str, Any]) -> None:
    expected = {
        "type": "started",
        "protocol_version": 2,
        "conversation_mode": NATIVE_CONVERSATION_MODE,
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


def _direct_answer_sdp(value: dict[str, Any]) -> str:
    """Validate the distinct protocol-v3 SDP answer without accepting extras."""
    if set(value) != {"type", "protocol_version", "transport"}:
        raise WebSocketError("bridge returned an incompatible WebRTC answer")
    if value.get("type") != "answer" or value.get("protocol_version") != 3:
        raise WebSocketError("bridge returned an incompatible WebRTC answer")
    transport = value.get("transport")
    if not isinstance(transport, dict) or set(transport) != {"type", "sdp"}:
        raise WebSocketError("bridge returned an incompatible WebRTC transport")
    sdp = transport.get("sdp")
    if transport.get("type") != "webrtc" or not isinstance(sdp, str) or not sdp:
        raise WebSocketError("bridge returned an invalid WebRTC SDP answer")
    if "\x00" in sdp or not sdp.startswith("v=0"):
        raise WebSocketError("bridge returned an invalid WebRTC SDP answer")
    return sdp


def _validate_direct_started(value: dict[str, Any]) -> None:
    """Require the content-free signaling-only protocol-v3 acknowledgement."""
    expected = {
        "type": "started",
        "version": "v3",
        "protocol_version": 3,
        "conversation_mode": NATIVE_CONVERSATION_MODE,
        "transport": "webrtc",
        "audio_over_bridge": False,
        "sideband_control": True,
    }
    if value != expected:
        raise WebSocketError("bridge returned an incompatible direct WebRTC protocol")


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
