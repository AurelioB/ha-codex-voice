"""aiortc peer owned by the ThirdReality device sidecar."""

from __future__ import annotations

import asyncio
import queue as thread_queue
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .protocol import CaptureAudio, PlaybackAudio, sanitize_provider_lifecycle

CAPTURE_SAMPLE_RATE = 16_000
PLAYBACK_SAMPLE_RATE = 24_000
PCM_SAMPLE_WIDTH = 2
MAX_CAPTURE_QUEUE_FRAMES = 32
# An offer-created replacement accepts capture while the bridge retires and
# renegotiates the provider. The 32 x 64 ms queue stays capped at 2.048 seconds;
# a separate 2.25-second timestamp proof permits one bounded scheduler pause
# and still rejects stale packets before admission and at RTP consumption.
MAX_CAPTURE_QUEUE_MILLISECONDS = 2_048
MAX_CAPTURE_AGE_MILLISECONDS = 2_250
_MAX_CAPTURE_QUEUE_SAMPLES = (
    CAPTURE_SAMPLE_RATE * MAX_CAPTURE_QUEUE_MILLISECONDS // 1_000
)
_MAX_CAPTURE_AGE_NANOSECONDS = MAX_CAPTURE_AGE_MILLISECONDS * 1_000_000
_MAX_CAPTURE_FUTURE_SKEW_NANOSECONDS = 100 * 1_000_000
MEDIA_QUIET_SECONDS = 0.120
MEDIA_FENCE_QUIET_SECONDS = 0.500
MEDIA_FENCE_MINIMUM_GUARD_SECONDS = 0.750
MEDIA_FENCE_MINIMUM_CAPTURE_SAMPLES = CAPTURE_SAMPLE_RATE * 250 // 1_000
MEDIA_FENCE_TIMEOUT_SECONDS = 5.0
MEDIA_FENCE_RECEIVER_HEARTBEAT_SECONDS = 0.020
MEDIA_FENCE_RECEIVER_MAX_TICK_SLIP_SECONDS = 0.010

try:
    import aiortc
    from aiortc import (
        MediaStreamTrack,
        RTCConfiguration,
        RTCPeerConnection,
        RTCSessionDescription,
    )
    from aiortc.jitterbuffer import JitterBuffer
    from aiortc.mediastreams import MediaStreamError
    from av import AudioFrame
    from av.audio.resampler import AudioResampler
except ImportError as import_error:  # Reported as one content-free error by runtime.
    aiortc = None
    MediaStreamTrack = object  # type: ignore[assignment,misc]
    RTCConfiguration = None  # type: ignore[assignment,misc]
    RTCPeerConnection = None  # type: ignore[assignment,misc]
    RTCSessionDescription = None  # type: ignore[assignment,misc]
    MediaStreamError = Exception  # type: ignore[assignment,misc]
    JitterBuffer = None  # type: ignore[assignment,misc]
    AudioFrame = None  # type: ignore[assignment,misc]
    AudioResampler = None  # type: ignore[assignment,misc]
    _IMPORT_ERROR: ImportError | None = import_error
else:
    _IMPORT_ERROR = None


class PeerError(RuntimeError):
    """Raised for one content-independent WebRTC peer failure."""


class PeerBackpressure(PeerError):
    """Raised instead of dropping microphone or playback frames."""


class CaptureAudioTrack(MediaStreamTrack):
    """Bounded, timestamp-preserving 16 kHz PCM source for aiortc."""

    kind = "audio"

    def __init__(
        self,
        *,
        on_consumed: Callable[[int], None] | None = None,
        on_fatal: Callable[[str], None] | None = None,
    ) -> None:
        """Create an empty bounded track on the active sidecar event loop."""
        if _IMPORT_ERROR is not None or AudioFrame is None:
            raise PeerError("WebRTC dependencies are unavailable") from _IMPORT_ERROR
        super().__init__()
        self._queue: asyncio.Queue[CaptureAudio | None] = asyncio.Queue(
            maxsize=MAX_CAPTURE_QUEUE_FRAMES
        )
        self._stopped = False
        self._first_sample_index: int | None = None
        self._last_sample_end: int | None = None
        self._last_capture_monotonic_ns: int | None = None
        self._queued_samples = 0
        self._consumed_samples = 0
        self._consumed_sample_end: int | None = None
        self._on_consumed = on_consumed
        self._on_fatal = on_fatal

    @property
    def consumed_samples(self) -> int:
        """Return capture samples handed to the WebRTC sender."""
        return self._consumed_samples

    @property
    def latest_sample_end(self) -> int | None:
        """Return the end of the newest capture packet accepted from the parent."""
        return self._last_sample_end

    @property
    def consumed_sample_end(self) -> int | None:
        """Return the end of the newest capture packet handed to the sender."""
        return self._consumed_sample_end

    @property
    def sender_sample_cursor(self) -> int | None:
        """Return the sender end, or the queued stream start before its first pull."""
        if self._consumed_sample_end is not None:
            return self._consumed_sample_end
        return self._first_sample_index

    def feed(self, value: CaptureAudio) -> None:
        """Queue one validated capture packet without blocking."""
        if self._stopped:
            raise PeerError("capture track is stopped")
        samples = len(value.pcm) // PCM_SAMPLE_WIDTH
        now_ns = time.monotonic_ns()
        if value.capture_monotonic_ns > now_ns + _MAX_CAPTURE_FUTURE_SKEW_NANOSECONDS:
            raise PeerError("capture timestamp is in the future")
        if now_ns - value.capture_monotonic_ns > _MAX_CAPTURE_AGE_NANOSECONDS:
            raise PeerBackpressure("capture packet exceeded its age bound")
        if (
            self._last_sample_end is not None
            and value.sample_index != self._last_sample_end
        ):
            raise PeerError("capture sample index is not contiguous")
        if (
            self._last_capture_monotonic_ns is not None
            and value.capture_monotonic_ns < self._last_capture_monotonic_ns
        ):
            raise PeerError("capture timestamp moved backwards")
        if self._queued_samples + samples > _MAX_CAPTURE_QUEUE_SAMPLES:
            raise PeerBackpressure("capture queue reached its duration bound")
        try:
            self._queue.put_nowait(value)
        except asyncio.QueueFull as exc:
            raise PeerBackpressure("capture queue reached its bound") from exc
        if self._first_sample_index is None:
            self._first_sample_index = value.sample_index
        self._queued_samples += samples
        self._last_sample_end = value.sample_index + samples
        self._last_capture_monotonic_ns = value.capture_monotonic_ns

    async def recv(self) -> Any:
        """Return the next timestamped frame without imposing a second 1x clock."""
        value = await self._queue.get()
        if value is None:
            raise MediaStreamError
        samples = len(value.pcm) // PCM_SAMPLE_WIDTH
        self._queued_samples -= samples
        if time.monotonic_ns() - value.capture_monotonic_ns > (
            _MAX_CAPTURE_AGE_NANOSECONDS
        ):
            # Offer-created peers can accumulate capture before the remote SDP
            # activates RTP. Admission freshness is therefore insufficient:
            # prove freshness again at the sender's actual consumption point.
            if self._on_fatal is not None:
                self._on_fatal("capture_audio_stale")
            self.stop()
            raise PeerBackpressure(
                "capture packet exceeded its age bound before RTP consumption"
            )
        assert AudioFrame is not None
        frame = AudioFrame(format="s16", layout="mono", samples=samples)
        frame.planes[0].update(value.pcm)
        frame.sample_rate = CAPTURE_SAMPLE_RATE
        first_sample_index = self._first_sample_index
        assert first_sample_index is not None
        frame.pts = value.sample_index - first_sample_index
        frame.time_base = Fraction(1, CAPTURE_SAMPLE_RATE)
        self._consumed_samples += samples
        self._consumed_sample_end = value.sample_index + samples
        if self._on_consumed is not None:
            self._on_consumed(self._consumed_samples)
        return frame

    def stop(self) -> None:
        """Wake a blocked sender and reject any later capture packet."""
        if self._stopped:
            return
        self._stopped = True
        super().stop()
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queued_samples = 0
        self._queue.put_nowait(None)


LifecycleEmitter = Callable[[dict[str, str | int]], None]
PlaybackEmitter = Callable[[PlaybackAudio], None]
StateEmitter = Callable[[str], None]
FatalEmitter = Callable[[str], None]


@dataclass(slots=True)
class _MediaFence:
    """One immutable-deadline local interruption media fence."""

    started_at: float
    deadline: float
    last_decoded_at: float
    required_consumed_end: int | None
    capture_complete: bool = False
    lifecycle_failed: bool = False
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class _ReceiverBarrier:
    """One receiver-owned request for a fresh decoded-RTP silence interval."""

    acknowledgement: asyncio.Future[None]
    not_before: float


@dataclass(frozen=True, slots=True)
class _TrackedRemoteItem:
    serial: int | None
    value: Any


class _TrackedRemoteQueue:
    """Serialize aiortc decoder production with receiver consumption/commit."""

    def __init__(self, queue: asyncio.Queue[Any]) -> None:
        self._queue = queue
        self._lock = threading.Lock()
        self._produced_serial = 0
        self._processed_serial = 0
        self._delivered_serial: int | None = None
        self._last_produced_at: float | None = None
        self._terminal_produced = False

    def _produce(self, value: Any) -> _TrackedRemoteItem:
        # aiortc 1.15 calls output_q.put(frame) synchronously in its decoder
        # worker before handing the coroutine to run_coroutine_threadsafe().
        # Recording here therefore cannot be hidden by an event-loop stall.
        with self._lock:
            if value is None:
                self._terminal_produced = True
                self._last_produced_at = time.monotonic()
                return _TrackedRemoteItem(None, None)
            self._produced_serial += 1
            self._last_produced_at = time.monotonic()
            return _TrackedRemoteItem(self._produced_serial, value)

    def put(self, value: Any) -> Coroutine[Any, Any, None]:
        """Return the underlying put coroutine after producer-side accounting."""
        return self._queue.put(self._produce(value))

    def put_nowait(self, value: Any) -> None:
        """Support deterministic event-loop tests with identical accounting."""
        self._queue.put_nowait(self._produce(value))

    async def get(self) -> Any:
        """Unwrap one item and retain its serial until peer processing ends."""
        item = await self._queue.get()
        if not isinstance(item, _TrackedRemoteItem):
            raise PeerError("remote queue tracker received an invalid item")
        if item.serial is None:
            return item.value
        with self._lock:
            if self._delivered_serial is not None:
                raise PeerError("remote queue tracker has concurrent deliveries")
            if item.serial != self._processed_serial + 1:
                raise PeerError("remote queue tracker sequence is not contiguous")
            self._delivered_serial = item.serial
        return item.value

    def mark_processed(self) -> None:
        """Publish completion only after muted/frame handling has succeeded."""
        with self._lock:
            if self._delivered_serial != self._processed_serial + 1:
                raise PeerError("remote queue tracker processing is not contiguous")
            self._processed_serial = self._delivered_serial
            self._delivered_serial = None

    def empty(self) -> bool:
        return self._queue.empty()

    def qsize(self) -> int:
        return self._queue.qsize()

    def quiet_and_drained(self, *, quiet_seconds: float) -> bool:
        """Return a thread-safe observation outside the final commit section."""
        with self._lock:
            return self._quiet_and_drained_locked(
                now=time.monotonic(),
                quiet_seconds=quiet_seconds,
            )

    @contextmanager
    def commit_guard(self) -> Iterator[tuple[int, int, float | None, bool]]:
        """Block decoder production across the final fence decision and write."""
        with self._lock:
            yield (
                self._produced_serial,
                self._processed_serial,
                self._last_produced_at,
                self._terminal_produced,
            )

    @staticmethod
    def snapshot_is_quiet_and_drained(
        snapshot: tuple[int, int, float | None, bool],
        *,
        quiet_seconds: float,
    ) -> bool:
        """Evaluate a snapshot while its caller retains the producer lock."""
        produced_serial, processed_serial, last_produced_at, terminal_produced = (
            snapshot
        )
        return (
            not terminal_produced
            and produced_serial == processed_serial
            and (
                last_produced_at is None
                or time.monotonic() - last_produced_at >= quiet_seconds
            )
        )

    def _quiet_and_drained_locked(self, *, now: float, quiet_seconds: float) -> bool:
        return (
            not self._terminal_produced
            and (self._produced_serial == self._processed_serial)
            and (
                self._last_produced_at is None
                or now - self._last_produced_at >= quiet_seconds
            )
        )


@dataclass(frozen=True, slots=True)
class _TrackedDecoderItem:
    serial: int | None
    value: Any


class _TrackedDecoderQueue:
    """Serialize encoded-frame production, in-flight decode, and commit."""

    def __init__(self, queue: thread_queue.Queue[Any]) -> None:
        self._queue = queue
        self._lock = threading.Lock()
        self._produced_serial = 0
        self._completed_serial = 0
        self._in_flight_serial: int | None = None
        self._last_produced_at: float | None = None
        self._terminal_produced = False

    def _produce(self, value: Any) -> _TrackedDecoderItem:
        with self._lock:
            if value is None:
                self._terminal_produced = True
                self._last_produced_at = time.monotonic()
                return _TrackedDecoderItem(None, None)
            self._produced_serial += 1
            self._last_produced_at = time.monotonic()
            return _TrackedDecoderItem(self._produced_serial, value)

    def put(
        self,
        value: Any,
        block: bool = True,
        timeout: float | None = None,
    ) -> None:
        self._queue.put(self._produce(value), block=block, timeout=timeout)

    def put_nowait(self, value: Any) -> None:
        self._queue.put_nowait(self._produce(value))

    def get(self, block: bool = True, timeout: float | None = None) -> Any:
        # decoder_worker calls get() again only after decode and every tracked
        # output_q.put() call completed synchronously in that worker thread.
        with self._lock:
            if self._in_flight_serial is not None:
                if self._in_flight_serial != self._completed_serial + 1:
                    raise PeerError("decoder completion sequence is not contiguous")
                self._completed_serial = self._in_flight_serial
                self._in_flight_serial = None
        item = self._queue.get(block=block, timeout=timeout)
        if not isinstance(item, _TrackedDecoderItem):
            raise PeerError("decoder queue tracker received an invalid item")
        if item.serial is None:
            return item.value
        with self._lock:
            if self._in_flight_serial is not None:
                raise PeerError("decoder queue tracker has concurrent work")
            if item.serial != self._completed_serial + 1:
                raise PeerError("decoder queue tracker sequence is not contiguous")
            self._in_flight_serial = item.serial
        return item.value

    def empty(self) -> bool:
        return self._queue.empty()

    def qsize(self) -> int:
        return self._queue.qsize()

    def quiet_and_drained(self, *, quiet_seconds: float) -> bool:
        with self._lock:
            return self._quiet_and_drained_locked(
                now=time.monotonic(),
                quiet_seconds=quiet_seconds,
            )

    @contextmanager
    def commit_guard(
        self,
    ) -> Iterator[tuple[int, int, int | None, float | None, bool]]:
        with self._lock:
            yield (
                self._produced_serial,
                self._completed_serial,
                self._in_flight_serial,
                self._last_produced_at,
                self._terminal_produced,
            )

    @staticmethod
    def snapshot_is_quiet_and_drained(
        snapshot: tuple[int, int, int | None, float | None, bool],
        *,
        quiet_seconds: float,
    ) -> bool:
        produced, completed, in_flight, last_produced_at, terminal = snapshot
        return (
            not terminal
            and in_flight is None
            and produced == completed
            and (
                last_produced_at is None
                or time.monotonic() - last_produced_at >= quiet_seconds
            )
        )

    def _quiet_and_drained_locked(self, *, now: float, quiet_seconds: float) -> bool:
        return (
            not self._terminal_produced
            and self._in_flight_serial is None
            and self._produced_serial == self._completed_serial
            and (
                self._last_produced_at is None
                or now - self._last_produced_at >= quiet_seconds
            )
        )


class DeviceWebRtcPeer:
    """Own one direct provider PeerConnection and sanitized event boundary."""

    def __init__(
        self,
        *,
        emit_lifecycle: LifecycleEmitter,
        emit_playback: PlaybackEmitter,
        emit_state: StateEmitter,
        emit_fatal: FatalEmitter,
    ) -> None:
        """Create one audio-only peer with content-free output callbacks."""
        if (
            _IMPORT_ERROR is not None
            or RTCConfiguration is None
            or RTCPeerConnection is None
            or AudioResampler is None
            or JitterBuffer is None
        ):
            raise PeerError("WebRTC dependencies are unavailable") from _IMPORT_ERROR
        self._emit_lifecycle = emit_lifecycle
        self._emit_playback = emit_playback
        self._emit_state = emit_state
        self._emit_fatal = emit_fatal
        self._closed = False
        self._failed = False
        self._generation = 0
        self._media_generation_open = False
        self._receiver_quiet = True
        self._muted = False
        self._media_activity_serial = 0
        self._media_quiet_timer: asyncio.TimerHandle | None = None
        self._fence_quiet_timer: asyncio.TimerHandle | None = None
        self._fence_timeout_timer: asyncio.TimerHandle | None = None
        self._fence_completion_task: asyncio.Task[None] | None = None
        self._fence: _MediaFence | None = None
        self._receiver_barriers: asyncio.Queue[_ReceiverBarrier] = asyncio.Queue()
        self._receiver_queue: _TrackedRemoteQueue | None = None
        self._decoder_queue: _TrackedDecoderQueue | None = None
        self._audio_receiver: Any | None = None
        self._remote_audio_track_seen = False
        self._playback_sample_index = 0
        self._consumer_tasks: set[asyncio.Task[None]] = set()
        self._ice_gathering_complete = asyncio.Event()
        self.input_track = CaptureAudioTrack(
            on_consumed=self._note_capture_consumed,
            on_fatal=self._safe_fatal,
        )
        self.pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
        self.pc.addTransceiver(self.input_track, direction="sendrecv")
        self.data_channel = self.pc.createDataChannel("oai-events", ordered=True)
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self.pc.on("icegatheringstatechange")
        def on_ice_gathering_state_change() -> None:
            if self.pc.iceGatheringState == "complete":
                self._ice_gathering_complete.set()

        @self.pc.on("connectionstatechange")
        async def on_connection_state_change() -> None:
            state = self.pc.connectionState
            if state == "connected":
                self._safe_state("connected")
            elif state in {"closed", "failed"} and not self._closed:
                self._safe_fatal("connection_failed")

        @self.pc.on("track")
        def on_track(track: Any) -> None:
            if getattr(track, "kind", None) != "audio" or self._remote_audio_track_seen:
                self._safe_fatal("unexpected_media_track")
                return
            if not self._install_remote_receiver_boundary(track):
                self._safe_fatal("unsupported_receiver_boundary")
                return
            self._remote_audio_track_seen = True
            task = asyncio.create_task(
                self._consume_remote_audio(track),
                name="codex-device-webrtc-audio",
            )
            self._consumer_tasks.add(task)
            task.add_done_callback(self._consumer_done)

        @self.data_channel.on("open")
        def on_data_open() -> None:
            self._safe_state("data.ready")

        @self.data_channel.on("message")
        def on_data_message(message: str | bytes) -> None:
            lifecycle = sanitize_provider_lifecycle(message)
            if lifecycle is None:
                return
            if self._is_provider_error(lifecycle):
                self._handle_provider_error(lifecycle)
                return
            lifecycle["generation"] = self._generation
            self._safe_lifecycle(lifecycle)

        @self.data_channel.on("close")
        def on_data_close() -> None:
            if not self._closed:
                self._safe_fatal("data_channel_closed")

    async def create_offer(self) -> str:
        """Create a host-candidate offer without a blocking public STUN probe."""
        if self._closed:
            raise PeerError("WebRTC peer is closed")
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        if self.pc.iceGatheringState != "complete":
            try:
                await asyncio.wait_for(self._ice_gathering_complete.wait(), timeout=10)
            except TimeoutError as exc:
                raise PeerError("ICE gathering timed out") from exc
        local_description = self.pc.localDescription
        sdp = getattr(local_description, "sdp", None)
        if not isinstance(sdp, str) or not sdp:
            raise PeerError("WebRTC did not create an SDP offer")
        return sdp

    async def set_answer(self, sdp: str) -> None:
        """Apply exactly one App Server SDP answer."""
        if self._closed or not sdp:
            raise PeerError("WebRTC answer cannot be applied")
        assert RTCSessionDescription is not None
        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=sdp, type="answer")
        )

    def _install_remote_queue_tracker(self, track: Any) -> bool:
        """Install the pinned aiortc 1.15 decoded-output serialization shim."""
        if aiortc is None or getattr(aiortc, "__version__", None) != "1.15.0":
            return False
        queue = getattr(track, "_queue", None)
        if isinstance(queue, _TrackedRemoteQueue):
            self._receiver_queue = queue
            return True
        if not isinstance(queue, asyncio.Queue):
            return False
        if queue.maxsize != 0 or not queue.empty():
            return False
        tracker = _TrackedRemoteQueue(queue)
        try:
            track._queue = tracker  # noqa: SLF001 - pinned aiortc 1.15 boundary.
        except (AttributeError, TypeError):
            return False
        if getattr(track, "_queue", None) is not tracker:
            return False
        self._receiver_queue = tracker
        return True

    def _install_remote_receiver_boundary(self, track: Any) -> bool:
        """Install every pinned pre-decoder and post-decoder fence boundary."""
        if not self._install_remote_queue_tracker(track):
            return False
        get_receivers = getattr(self.pc, "getReceivers", None)
        if not callable(get_receivers):
            return False
        receivers = [
            receiver
            for receiver in get_receivers()
            if getattr(receiver, "track", None) is track
        ]
        if len(receivers) != 1:
            return False
        receiver = receivers[0]
        decoder_queue = getattr(
            receiver,
            "_RTCRtpReceiver__decoder_queue",
            None,
        )
        if isinstance(decoder_queue, _TrackedDecoderQueue):
            decoder_tracker = decoder_queue
        else:
            if not isinstance(decoder_queue, thread_queue.Queue):
                return False
            if decoder_queue.maxsize != 0 or not decoder_queue.empty():
                return False
            decoder_tracker = _TrackedDecoderQueue(decoder_queue)
            try:
                receiver._RTCRtpReceiver__decoder_queue = decoder_tracker  # noqa: SLF001
            except (AttributeError, TypeError):
                return False
            if (
                getattr(receiver, "_RTCRtpReceiver__decoder_queue", None)
                is not decoder_tracker
            ):
                return False
        jitter_buffer = getattr(
            receiver,
            "_RTCRtpReceiver__jitter_buffer",
            None,
        )
        if not self._is_supported_empty_jitter_buffer(jitter_buffer):
            return False
        self._decoder_queue = decoder_tracker
        self._audio_receiver = receiver
        return True

    @staticmethod
    def _is_supported_empty_jitter_buffer(value: Any) -> bool:
        return (
            JitterBuffer is not None
            and isinstance(value, JitterBuffer)
            and value.capacity == 16
            and getattr(value, "_prefetch", None) == 4
            and getattr(value, "_is_video", None) is False
            and getattr(value, "_origin", None) is None
            and all(packet is None for packet in getattr(value, "_packets", [object()]))
        )

    def _reset_receiver_jitter_buffer(self) -> bool:
        """Discard the verified pinned audio jitter tail at final commit."""
        receiver = self._audio_receiver
        if receiver is None or JitterBuffer is None:
            return False
        current = getattr(receiver, "_RTCRtpReceiver__jitter_buffer", None)
        if not (
            isinstance(current, JitterBuffer)
            and current.capacity == 16
            and getattr(current, "_prefetch", None) == 4
            and getattr(current, "_is_video", None) is False
        ):
            return False
        replacement = JitterBuffer(capacity=16, prefetch=4)
        try:
            receiver._RTCRtpReceiver__jitter_buffer = replacement  # noqa: SLF001
        except (AttributeError, TypeError):
            return False
        return getattr(receiver, "_RTCRtpReceiver__jitter_buffer", None) is replacement

    def feed_capture(self, value: CaptureAudio) -> None:
        """Submit one timestamped microphone packet."""
        self.input_track.feed(value)

    def interrupt_response(self) -> None:
        """Fence local media while Frameless Bidi server VAD interrupts output."""
        if self._closed:
            raise PeerError("WebRTC peer is closed")
        # The subscription-backed Codex surface is Frameless Bidi, not the
        # public Realtime v2 client-event dialect. Its published outbound
        # schema has no response.cancel or output_audio_buffer.clear, and the
        # live provider rejects either as invalid_value(type). The ChatGPT-like
        # interruption contract is server-VAD-owned: keep sending capture,
        # kill local paplay immediately in the parent, mute decoded RTP here,
        # and require a fresh receiver-quiescence proof before unmuting.
        self._begin_local_fence()

    async def stop(self) -> None:
        """Close the peer and every owned media consumer."""
        self._cancel_timer("_media_quiet_timer")
        self._cancel_timer("_fence_quiet_timer")
        self._cancel_timer("_fence_timeout_timer")
        completion_task = self._fence_completion_task
        if completion_task is not None:
            completion_task.cancel()
            self._fence_completion_task = None
        if self._closed:
            return
        self._closed = True
        self.input_track.stop()
        await self.pc.close()
        for task in tuple(self._consumer_tasks):
            task.cancel()
        tasks = list(self._consumer_tasks)
        if completion_task is not None:
            tasks.append(completion_task)
        await asyncio.gather(*tasks, return_exceptions=True)
        self._consumer_tasks.clear()

    @staticmethod
    def _is_provider_error(lifecycle: dict[str, str | int]) -> bool:
        event_type = lifecycle["event_type"]
        assert isinstance(event_type, str)
        return event_type in {"error", "invalid_request_error"} or event_type.endswith(
            "_error"
        )

    def _handle_provider_error(self, lifecycle: dict[str, str | int]) -> None:
        """Forward only allowlisted classification, then fail the provider session."""
        self._fatal_provider_error(lifecycle, "provider_error")

    def _fatal_provider_error(
        self,
        lifecycle: dict[str, str | int],
        code: str,
    ) -> None:
        """Emit only allowlisted provider classification before failing closed."""
        values: dict[str, str | int] = {
            "event_type": "error",
            "generation": self._generation,
        }
        for key in ("error_event_id", "error_type", "error_code", "error_param"):
            value = lifecycle.get(key)
            if isinstance(value, str):
                values[key] = value
        self._safe_lifecycle(values)
        self._safe_fatal(code)

    def _begin_local_fence(self) -> None:
        """Start one local AEC-authorized empirical media fence."""
        self._muted = True
        if self._fence is not None:
            # The local IPC transport is reliable and ordered. Treat a duplicate
            # token as idempotent so neither the quiet proof nor hard deadline
            # can be extended indefinitely.
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        sender_cursor = self.input_track.sender_sample_cursor
        latest_fed_end = self.input_track.latest_sample_end
        required_consumed_end = (
            max(
                latest_fed_end,
                sender_cursor + MEDIA_FENCE_MINIMUM_CAPTURE_SAMPLES,
            )
            if sender_cursor is not None and latest_fed_end is not None
            else None
        )
        self._fence = _MediaFence(
            started_at=now,
            deadline=now + MEDIA_FENCE_TIMEOUT_SECONDS,
            last_decoded_at=now,
            required_consumed_end=required_consumed_end,
        )
        self._arm_fence_quiet_timer()
        self._arm_fence_timeout()

    def _start_receiver_quiet_window(self) -> None:
        """Measure one new uninterrupted receiver-quiescence interval."""
        self._receiver_quiet = False
        self._media_activity_serial += 1
        serial = self._media_activity_serial
        self._cancel_timer("_media_quiet_timer")
        loop = asyncio.get_running_loop()
        self._media_quiet_timer = loop.call_later(
            MEDIA_QUIET_SECONDS,
            self._mark_receiver_quiet,
            serial,
        )

    def _maybe_complete_fence(self) -> None:
        fence = self._fence
        if (
            fence is None
            or fence.timed_out
            or fence.lifecycle_failed
            or self._closed
            or self._failed
        ):
            return
        now = asyncio.get_running_loop().time()
        if now >= fence.deadline:
            self._fence_timed_out(fence)
            return
        if self._capture_proof_holds(fence):
            fence.capture_complete = True
        if not fence.capture_complete:
            return
        if self._fence_completion_task is not None:
            return
        self._fence_completion_task = asyncio.create_task(
            self._complete_fence_after_receiver_drain(fence),
            name="codex-device-webrtc-fence-drain",
        )

    async def _complete_fence_after_receiver_drain(self, fence: _MediaFence) -> None:
        """Wait for receiver-owned fresh RTP silence before unmuting."""
        current_task = asyncio.current_task()
        try:
            await self._receiver_drain_barrier(
                not_before=(fence.started_at + MEDIA_FENCE_MINIMUM_GUARD_SECONDS),
            )
            if (
                self._closed
                or self._failed
                or fence is not self._fence
                or fence.timed_out
                or fence.lifecycle_failed
            ):
                return
            now = asyncio.get_running_loop().time()
            if now >= fence.deadline:
                self._fence_timed_out(fence)
                return
            if now < fence.started_at + MEDIA_FENCE_MINIMUM_GUARD_SECONDS:
                return
            if now < fence.last_decoded_at + MEDIA_FENCE_QUIET_SECONDS:
                return
            if not fence.capture_complete:
                return
            receiver_queue = self._receiver_queue
            decoder_queue = self._decoder_queue
            if receiver_queue is None or decoder_queue is None:
                return
            # The pinned decoder worker records production under this lock
            # before scheduling its queue-put coroutine. Keep the lock through
            # both lifecycle writes and unmute, so a producer callback hidden
            # behind event-loop lag cannot cross the commit decision.
            with (
                decoder_queue.commit_guard() as decoder_snapshot,
                receiver_queue.commit_guard() as receiver_snapshot,
            ):
                if (
                    self._closed
                    or self._failed
                    or fence is not self._fence
                    or fence.timed_out
                    or fence.lifecycle_failed
                ):
                    return
                if not decoder_queue.snapshot_is_quiet_and_drained(
                    decoder_snapshot,
                    quiet_seconds=MEDIA_FENCE_QUIET_SECONDS,
                ):
                    return
                if not receiver_queue.snapshot_is_quiet_and_drained(
                    receiver_snapshot,
                    quiet_seconds=MEDIA_FENCE_QUIET_SECONDS,
                ):
                    return
                if not self._reset_receiver_jitter_buffer():
                    self._safe_fatal("receiver_boundary_reset_failed")
                    return
                if not self._force_media_quiet():
                    self._fail_fence_lifecycle(fence)
                    return
                if asyncio.get_running_loop().time() >= fence.deadline:
                    self._fence_timed_out(fence)
                    return
                if not self._safe_lifecycle(
                    {
                        "event_type": "interrupt.fenced",
                        "generation": self._generation,
                    }
                ):
                    self._fail_fence_lifecycle(fence)
                    return
                # A successful ordered interrupt.fenced write is the commit
                # point. Reclassifying afterward could expose both success and
                # fatal timeout for the same fence.
                self._cancel_timer("_fence_quiet_timer")
                self._cancel_timer("_fence_timeout_timer")
                self._fence = None
                self._muted = False
        finally:
            if self._fence_completion_task is current_task:
                self._fence_completion_task = None

    async def _receiver_drain_barrier(self, *, not_before: float) -> None:
        """Ask the sole receiver for one new uninterrupted silence interval."""
        acknowledgement = asyncio.get_running_loop().create_future()
        self._receiver_barriers.put_nowait(
            _ReceiverBarrier(
                acknowledgement=acknowledgement,
                not_before=not_before,
            )
        )
        await acknowledgement

    def _capture_proof_holds(self, fence: _MediaFence) -> bool:
        """Require post-token progress through the pre-token capture watermark."""
        required_consumed_end = fence.required_consumed_end
        consumed_end = self.input_track.consumed_sample_end
        return (
            required_consumed_end is not None
            and consumed_end is not None
            and consumed_end >= required_consumed_end
        )

    def _fail_fence_lifecycle(self, fence: _MediaFence) -> None:
        """Seal a fence after an internal lifecycle IPC failure until timeout."""
        if fence is not self._fence or fence.timed_out:
            return
        fence.lifecycle_failed = True
        self._cancel_timer("_fence_quiet_timer")

    def _note_receiver_audio(self) -> None:
        """Reset the interruption-fence quiet window for any decoded RTP."""
        fence = self._fence
        if fence is not None and not fence.timed_out:
            fence.last_decoded_at = asyncio.get_running_loop().time()
            self._arm_fence_quiet_timer()

    def _note_capture_consumed(self, _consumed_samples: int) -> None:
        """Re-evaluate a fence when actual WebRTC capture progress advances."""
        self._maybe_complete_fence()

    def _arm_fence_quiet_timer(self) -> None:
        """Check after the overlapping guard and decoded-RTP quiet windows."""
        fence = self._fence
        if (
            fence is None
            or fence.timed_out
            or fence.lifecycle_failed
            or self._closed
            or self._failed
        ):
            return
        self._cancel_timer("_fence_quiet_timer")
        loop = asyncio.get_running_loop()
        ready_at = max(
            fence.started_at + MEDIA_FENCE_MINIMUM_GUARD_SECONDS,
            fence.last_decoded_at + MEDIA_FENCE_QUIET_SECONDS,
        )
        self._fence_quiet_timer = loop.call_later(
            max(0.0, ready_at - loop.time()),
            self._fence_quiet_elapsed,
            fence,
        )

    def _fence_quiet_elapsed(self, fence: _MediaFence) -> None:
        self._fence_quiet_timer = None
        if self._closed or self._failed or fence is not self._fence or fence.timed_out:
            return
        self._maybe_complete_fence()

    def _mark_receiver_quiet(self, serial: int) -> None:
        if self._closed or self._failed or serial != self._media_activity_serial:
            return
        self._media_quiet_timer = None
        self._receiver_quiet = True
        if not self._close_media_generation() and self._fence is not None:
            self._fail_fence_lifecycle(self._fence)

    def _force_media_quiet(self) -> bool:
        """Close an open generation before acknowledging a completed fence."""
        self._cancel_timer("_media_quiet_timer")
        self._media_activity_serial += 1
        self._receiver_quiet = True
        return self._close_media_generation()

    def _close_media_generation(self) -> bool:
        if not self._media_generation_open:
            return True
        if not self._safe_lifecycle(
            {
                "event_type": "media.quiet",
                "generation": self._generation,
            }
        ):
            return False
        self._media_generation_open = False
        return True

    def _arm_fence_timeout(self) -> None:
        if self._fence_timeout_timer is not None or self._closed or self._failed:
            return
        loop = asyncio.get_running_loop()
        fence = self._fence
        if fence is None:
            return
        self._fence_timeout_timer = loop.call_at(
            fence.deadline,
            self._fence_timed_out,
            fence,
        )

    def _fence_timed_out(self, fence: _MediaFence | None) -> None:
        if (
            self._closed
            or self._failed
            or fence is None
            or self._fence is not fence
            or fence.timed_out
        ):
            return
        self._cancel_timer("_fence_timeout_timer")
        fence.timed_out = True
        self._cancel_timer("_fence_quiet_timer")
        completion_task = self._fence_completion_task
        if completion_task is not None:
            if completion_task is not asyncio.current_task():
                completion_task.cancel()
            self._fence_completion_task = None
        code = (
            "media_fence_timeout"
            if fence.capture_complete
            else "media_fence_capture_timeout"
        )
        self._safe_fatal(code)

    def _cancel_timer(self, attribute: str) -> None:
        timer = getattr(self, attribute)
        if timer is not None:
            timer.cancel()
            setattr(self, attribute, None)

    async def _consume_remote_audio(self, track: Any) -> None:
        assert AudioResampler is not None
        if self._receiver_queue is None and not self._install_remote_queue_tracker(
            track
        ):
            raise PeerError("remote audio queue cannot be serialized")
        resampler = self._new_remote_audio_resampler()
        receive_task = asyncio.create_task(
            track.recv(),
            name="codex-device-webrtc-receiver-frame",
        )
        barrier_task = asyncio.create_task(
            self._receiver_barriers.get(),
            name="codex-device-webrtc-receiver-barrier",
        )
        pending_barriers: list[_ReceiverBarrier] = []
        responsive_quiet_seconds = 0.0
        try:
            while True:
                timeout: float | None = None
                if pending_barriers:
                    loop = asyncio.get_running_loop()
                    now = loop.time()
                    remaining_quiet = max(
                        0.0,
                        MEDIA_FENCE_QUIET_SECONDS - responsive_quiet_seconds,
                    )
                    remaining_guard = max(
                        0.0,
                        max(barrier.not_before for barrier in pending_barriers) - now,
                    )
                    remaining = max(remaining_quiet, remaining_guard)
                    timeout = min(
                        MEDIA_FENCE_RECEIVER_HEARTBEAT_SECONDS,
                        remaining or MEDIA_FENCE_RECEIVER_HEARTBEAT_SECONDS,
                    )
                wait_started_at = asyncio.get_running_loop().time()
                done, _ = await asyncio.wait(
                    {receive_task, barrier_task},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                wait_finished_at = asyncio.get_running_loop().time()
                if barrier_task in done:
                    pending_barriers.append(barrier_task.result())
                    # Freshness begins only when the sole consumer observes the
                    # request, not when another task enqueues it.
                    responsive_quiet_seconds = 0.0
                    barrier_task = asyncio.create_task(
                        self._receiver_barriers.get(),
                        name="codex-device-webrtc-receiver-barrier",
                    )
                if receive_task in done:
                    self._consume_remote_frame(receive_task.result(), resampler)
                    receiver_queue = self._receiver_queue
                    if receiver_queue is None:
                        raise PeerError("remote audio queue tracker disappeared")
                    receiver_queue.mark_processed()
                    receive_task = asyncio.create_task(
                        track.recv(),
                        name="codex-device-webrtc-receiver-frame",
                    )
                    if pending_barriers:
                        responsive_quiet_seconds = 0.0
                if not pending_barriers:
                    continue

                if not done:
                    elapsed = max(0.0, wait_finished_at - wait_started_at)
                    assert timeout is not None
                    if elapsed > timeout + MEDIA_FENCE_RECEIVER_MAX_TICK_SLIP_SECONDS:
                        # Wall time during a stalled loop is not receiver-owned
                        # silence. Decoder-thread callbacks can be queued but
                        # not yet reflected by recv(), so require the complete
                        # fresh interval again. Repeated lag reaches the fixed
                        # fence timeout and remains muted.
                        responsive_quiet_seconds = 0.0
                    else:
                        responsive_quiet_seconds = min(
                            MEDIA_FENCE_QUIET_SECONDS,
                            responsive_quiet_seconds + elapsed,
                        )
                if responsive_quiet_seconds < MEDIA_FENCE_QUIET_SECONDS:
                    continue
                if wait_finished_at < max(
                    barrier.not_before for barrier in pending_barriers
                ):
                    continue
                # If recv completed between asyncio.wait() returning and this
                # check, process it on the next iteration and restart silence.
                if receive_task.done():
                    continue
                receiver_queue = self._receiver_queue
                decoder_queue = self._decoder_queue
                if (
                    receiver_queue is None
                    or decoder_queue is None
                    or not receiver_queue.quiet_and_drained(
                        quiet_seconds=MEDIA_FENCE_QUIET_SECONDS
                    )
                    or not decoder_queue.quiet_and_drained(
                        quiet_seconds=MEDIA_FENCE_QUIET_SECONDS
                    )
                ):
                    # A decoder worker may have produced a frame whose queue-put
                    # coroutine has not run yet. Its serial is visible here even
                    # though recv() is not done, so never acknowledge past it.
                    responsive_quiet_seconds = 0.0
                    continue
                # PyAV resampling retains a short filter tail. Discard the
                # receiver-owned instance before acknowledging so muted
                # pre-fence samples cannot flush into the first unmuted frame.
                # The producer serial is rechecked under lock at final commit;
                # a racing frame forces another complete barrier and reset.
                resampler = self._new_remote_audio_resampler()
                for barrier in pending_barriers:
                    if not barrier.acknowledgement.done():
                        barrier.acknowledgement.set_result(None)
                pending_barriers.clear()
                responsive_quiet_seconds = 0.0
        except MediaStreamError:
            if not self._closed:
                raise PeerError("remote audio track ended") from None
        finally:
            for task in (receive_task, barrier_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(receive_task, barrier_task, return_exceptions=True)
            for barrier in pending_barriers:
                if not barrier.acknowledgement.done():
                    barrier.acknowledgement.cancel()

    @staticmethod
    def _new_remote_audio_resampler() -> Any:
        """Create one receiver-owned resampler with no prior filter state."""
        assert AudioResampler is not None
        return AudioResampler(
            format="s16",
            layout="mono",
            rate=PLAYBACK_SAMPLE_RATE,
        )

    def _consume_remote_frame(self, frame: Any, resampler: Any) -> None:
        """Account for and emit one receiver-owned decoded audio frame."""
        self._note_receiver_audio()
        converted = resampler.resample(frame)
        frames = converted if isinstance(converted, list) else [converted]
        for output in frames:
            if output is None:
                continue
            size = output.samples * PCM_SAMPLE_WIDTH
            pcm = bytes(output.planes[0])[:size]
            if not pcm or self._muted or not any(pcm):
                continue
            # The provider keeps its remote RTP track alive with exact digital
            # silence between responses. Only signal-bearing PCM represents
            # audible media: silent keepalive frames must not open a generation,
            # feed playback, arm local barge-in, or extend semantic activity.
            self._start_receiver_quiet_window()
            if not self._media_generation_open:
                self._generation += 1
                self._media_generation_open = True
                if not self._safe_lifecycle(
                    {
                        "event_type": "media.started",
                        "generation": self._generation,
                    }
                ):
                    self._media_generation_open = False
                    continue
            media_timestamp = self._media_timestamp(output)
            packet = PlaybackAudio(
                generation=self._generation,
                sample_index=self._playback_sample_index,
                media_timestamp=media_timestamp,
                pcm=pcm,
            )
            self._emit_playback(packet)
            self._playback_sample_index += output.samples

    def _consumer_done(self, task: asyncio.Task[None]) -> None:
        self._consumer_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None and not self._closed:
            self._safe_fatal("remote_audio_failed")

    def _media_timestamp(self, frame: Any) -> int:
        pts = getattr(frame, "pts", None)
        time_base = getattr(frame, "time_base", None)
        if isinstance(pts, int) and time_base is not None:
            value = int(pts * time_base * PLAYBACK_SAMPLE_RATE)
            if value >= 0:
                return value
        return self._playback_sample_index

    def _safe_lifecycle(self, values: dict[str, str | int]) -> bool:
        if self._failed:
            return False
        try:
            self._emit_lifecycle(values)
        except Exception:  # noqa: BLE001 - callback boundary must fail closed.
            self._safe_fatal("lifecycle_output_failed")
            return False
        return True

    def _safe_state(self, state: str) -> None:
        if self._failed:
            return
        try:
            self._emit_state(state)
        except Exception:  # noqa: BLE001 - callback boundary must fail closed.
            self._safe_fatal("state_output_failed")

    def _safe_fatal(self, code: str) -> None:
        if self._failed:
            return
        self._failed = True
        self._muted = True
        self._cancel_timer("_media_quiet_timer")
        self._cancel_timer("_fence_quiet_timer")
        self._cancel_timer("_fence_timeout_timer")
        with suppress(Exception):
            self._emit_fatal(code)
