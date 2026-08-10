"""aiortc peer owned by the ThirdReality device sidecar."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .protocol import CaptureAudio, PlaybackAudio, sanitize_provider_lifecycle

CAPTURE_SAMPLE_RATE = 16_000
PLAYBACK_SAMPLE_RATE = 24_000
PCM_SAMPLE_WIDTH = 2
MAX_CAPTURE_QUEUE_FRAMES = 32
MAX_CAPTURE_QUEUE_MILLISECONDS = 1_000
MAX_CAPTURE_AGE_MILLISECONDS = 1_500
_MAX_CAPTURE_QUEUE_SAMPLES = (
    CAPTURE_SAMPLE_RATE * MAX_CAPTURE_QUEUE_MILLISECONDS // 1_000
)
_MAX_CAPTURE_AGE_NANOSECONDS = MAX_CAPTURE_AGE_MILLISECONDS * 1_000_000
_MAX_CAPTURE_FUTURE_SKEW_NANOSECONDS = 100 * 1_000_000
MEDIA_QUIET_SECONDS = 0.120
MEDIA_FENCE_TIMEOUT_SECONDS = 1.0
_RESPONSE_TERMINAL_EVENT_TYPES = frozenset(
    {
        "response.cancelled",
        "response.completed",
        "response.done",
    }
)
_RESPONSE_TERMINAL_STATUSES = frozenset(
    {"completed", "cancelled", "failed", "incomplete"}
)

try:
    from aiortc import (
        MediaStreamTrack,
        RTCConfiguration,
        RTCPeerConnection,
        RTCSessionDescription,
    )
    from aiortc.mediastreams import MediaStreamError
    from av import AudioFrame
    from av.audio.resampler import AudioResampler
except ImportError as import_error:  # Reported as one content-free error by runtime.
    MediaStreamTrack = object  # type: ignore[assignment,misc]
    RTCConfiguration = None  # type: ignore[assignment,misc]
    RTCPeerConnection = None  # type: ignore[assignment,misc]
    RTCSessionDescription = None  # type: ignore[assignment,misc]
    MediaStreamError = Exception  # type: ignore[assignment,misc]
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

    def __init__(self) -> None:
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
        self._queued_samples -= len(value.pcm) // PCM_SAMPLE_WIDTH
        assert AudioFrame is not None
        samples = len(value.pcm) // PCM_SAMPLE_WIDTH
        frame = AudioFrame(format="s16", layout="mono", samples=samples)
        frame.planes[0].update(value.pcm)
        frame.sample_rate = CAPTURE_SAMPLE_RATE
        first_sample_index = self._first_sample_index
        assert first_sample_index is not None
        frame.pts = value.sample_index - first_sample_index
        frame.time_base = Fraction(1, CAPTURE_SAMPLE_RATE)
        return frame

    def stop(self) -> None:
        """Wake a blocked sender and reject any later capture packet."""
        if self._stopped:
            return
        self._stopped = True
        super().stop()
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            while not self._queue.empty():
                self._queue.get_nowait()
            self._queue.put_nowait(None)


LifecycleEmitter = Callable[[dict[str, str | int]], None]
PlaybackEmitter = Callable[[PlaybackAudio], None]
StateEmitter = Callable[[str], None]
FatalEmitter = Callable[[str], None]


@dataclass(slots=True)
class _MediaFence:
    """One interruption awaiting control settlement and receiver quiet."""

    response_id: str | None
    cancel_event_id: str | None
    clear_event_id: str | None
    control_complete: bool


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
        ):
            raise PeerError("WebRTC dependencies are unavailable") from _IMPORT_ERROR
        self._emit_lifecycle = emit_lifecycle
        self._emit_playback = emit_playback
        self._emit_state = emit_state
        self._emit_fatal = emit_fatal
        self._closed = False
        self._generation = 0
        self._media_generation_open = False
        self._receiver_quiet = True
        self._muted = False
        self._media_activity_serial = 0
        self._media_quiet_timer: asyncio.TimerHandle | None = None
        self._fence_timeout_timer: asyncio.TimerHandle | None = None
        self._fence: _MediaFence | None = None
        self._client_event_sequence = 0
        self._recoverable_cancel_event_ids: set[str] = set()
        self._response_in_progress = False
        self._response_id: str | None = None
        self._output_active = False
        self._output_state = "unknown"
        self._output_response_id: str | None = None
        self._playback_sample_index = 0
        self._consumer_tasks: set[asyncio.Task[None]] = set()
        self._ice_gathering_complete = asyncio.Event()
        self.input_track = CaptureAudioTrack()
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
            if getattr(track, "kind", None) != "audio":
                self._safe_fatal("unexpected_media_track")
                return
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
            self._observe_provider_state(lifecycle)
            lifecycle["generation"] = self._generation
            self._safe_lifecycle(lifecycle)
            self._maybe_complete_fence()

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

    def feed_capture(self, value: CaptureAudio) -> None:
        """Submit one timestamped microphone packet."""
        self.input_track.feed(value)

    def cancel_response(self, response_id: str | None = None) -> None:
        """Send only the provider's bounded cancellation control."""
        if self._closed or getattr(self.data_channel, "readyState", None) != "open":
            raise PeerError("WebRTC data channel is not ready")
        event_id = self._next_client_event_id("cancel")
        message: dict[str, str] = {
            "type": "response.cancel",
            "event_id": event_id,
        }
        if response_id is not None:
            message["response_id"] = response_id
        try:
            self.data_channel.send(
                json.dumps(message, ensure_ascii=True, separators=(",", ":"))
            )
        except Exception as exc:
            raise PeerError("WebRTC cancellation send failed") from exc
        self._remember_recoverable_cancel(event_id)

    def interrupt_response(self, response_id: str | None = None) -> None:
        """Fence media, then cancel and/or clear only active provider state."""
        if self._closed or getattr(self.data_channel, "readyState", None) != "open":
            raise PeerError("WebRTC data channel is not ready")
        if self._fence is not None:
            self._muted = True
            return

        # RTP and SCTP are independently ordered. Decoded media can arrive
        # before response/output lifecycle, so actual receiver activity must
        # be sufficient to stop and clear provider output. Do not attach a
        # stale identifier when lifecycle has not established the active
        # response yet; an unkeyed cancel targets the default conversation.
        media_active = self._media_generation_open or not self._receiver_quiet
        output_lifecycle_unknown = self._output_state == "unknown"
        cancel_needed = self._response_in_progress or (
            media_active and output_lifecycle_unknown
        )
        clear_needed = self._output_active or (
            media_active and output_lifecycle_unknown
        )
        cancel_event_id = (
            self._next_client_event_id("cancel") if cancel_needed else None
        )
        clear_event_id = self._next_client_event_id("clear") if clear_needed else None
        target_response_id = self._output_response_id or (
            self._response_id if self._response_in_progress else None
        )
        if target_response_id is None and not media_active:
            target_response_id = response_id
        self._begin_fence(
            response_id=target_response_id,
            cancel_event_id=cancel_event_id,
            clear_event_id=clear_event_id,
            control_complete=cancel_event_id is None and clear_event_id is None,
        )

        controls: list[dict[str, str]] = []
        if cancel_event_id is not None:
            cancellation = {
                "type": "response.cancel",
                "event_id": cancel_event_id,
            }
            cancel_target = target_response_id
            if cancel_target is not None:
                cancellation["response_id"] = cancel_target
            controls.append(cancellation)
        if clear_event_id is not None:
            controls.append(
                {
                    "type": "output_audio_buffer.clear",
                    "event_id": clear_event_id,
                }
            )
        try:
            for control in controls:
                self.data_channel.send(
                    json.dumps(control, ensure_ascii=True, separators=(",", ":"))
                )
        except Exception as exc:
            raise PeerError("WebRTC interruption send failed") from exc
        if cancel_event_id is not None:
            self._remember_recoverable_cancel(cancel_event_id)
        self._maybe_complete_fence()

    async def stop(self) -> None:
        """Close the peer and every owned media consumer."""
        if self._closed:
            return
        self._closed = True
        self._cancel_timer("_media_quiet_timer")
        self._cancel_timer("_fence_timeout_timer")
        self.input_track.stop()
        await self.pc.close()
        for task in tuple(self._consumer_tasks):
            task.cancel()
        await asyncio.gather(*self._consumer_tasks, return_exceptions=True)
        self._consumer_tasks.clear()

    def _observe_provider_state(self, lifecycle: dict[str, str | int]) -> None:
        """Track response and output controls without defining media generations."""
        event_type = lifecycle["event_type"]
        assert isinstance(event_type, str)
        response_id = lifecycle.get("response_id")
        assert response_id is None or isinstance(response_id, str)
        response_status = lifecycle.get("response_status")
        assert response_status is None or isinstance(response_status, str)

        if event_type in {"response.created", "response.started"} or (
            response_status == "in_progress"
        ):
            if not self._output_active:
                self._output_state = "unknown"
            self._response_in_progress = True
            if response_id is not None:
                self._response_id = response_id
        elif event_type in _RESPONSE_TERMINAL_EVENT_TYPES or (
            response_status in _RESPONSE_TERMINAL_STATUSES
        ):
            if self._correlates(self._response_id, response_id):
                self._response_in_progress = False

        if event_type in {"output_audio_buffer.started", "speaking.started"}:
            self._output_active = True
            self._output_state = "active"
            if response_id is not None:
                self._output_response_id = response_id

        fence = self._fence
        if event_type == "output_audio_buffer.cleared":
            if (
                fence is not None
                and fence.clear_event_id is not None
                and self._correlates(fence.response_id, response_id)
            ):
                fence.control_complete = True
            if self._correlates(self._output_response_id, response_id):
                self._output_active = False
                self._output_state = "stopped"
                self._output_response_id = None
        elif event_type in {"output_audio_buffer.stopped", "speaking.stopped"}:
            if self._correlates(self._output_response_id, response_id):
                self._output_active = False
                self._output_state = "stopped"
                self._output_response_id = None

        if (
            fence is not None
            and fence.cancel_event_id is not None
            and fence.clear_event_id is None
            and (
                event_type in _RESPONSE_TERMINAL_EVENT_TYPES
                or response_status in _RESPONSE_TERMINAL_STATUSES
            )
            and self._correlates(fence.response_id, response_id)
        ):
            fence.control_complete = True
            self._recoverable_cancel_event_ids.discard(fence.cancel_event_id)

        if event_type == "input_audio_buffer.speech_started" and self._fence is None:
            self._begin_fence(
                response_id=self._output_response_id or self._response_id,
                cancel_event_id=None,
                clear_event_id=None,
                control_complete=True,
            )

    @staticmethod
    def _correlates(expected: str | None, observed: str | None) -> bool:
        """Accept an unkeyed event only when no stronger key is known."""
        return observed == expected if expected is not None else True

    @staticmethod
    def _is_provider_error(lifecycle: dict[str, str | int]) -> bool:
        event_type = lifecycle["event_type"]
        assert isinstance(event_type, str)
        return event_type in {"error", "invalid_request_error"} or event_type.endswith(
            "_error"
        )

    def _handle_provider_error(self, lifecycle: dict[str, str | int]) -> None:
        """Classify provider errors solely through generated causal event IDs."""
        error_event_id = lifecycle.get("error_event_id")
        if not isinstance(error_event_id, str):
            self._safe_fatal("provider_error")
            return
        fence = self._fence
        if (
            fence is not None
            and fence.clear_event_id is not None
            and error_event_id == fence.clear_event_id
        ):
            self._safe_fatal("output_clear_failed")
            return
        if error_event_id not in self._recoverable_cancel_event_ids:
            self._safe_fatal("provider_error")
            return

        self._recoverable_cancel_event_ids.discard(error_event_id)
        self._response_in_progress = False
        if fence is not None and error_event_id == fence.cancel_event_id:
            if fence.clear_event_id is None:
                fence.control_complete = True
            self._maybe_complete_fence()

    def _next_client_event_id(self, operation: str) -> str:
        self._client_event_sequence += 1
        return f"codex_device_{operation}_{self._client_event_sequence}"

    def _remember_recoverable_cancel(self, event_id: str) -> None:
        self._recoverable_cancel_event_ids.add(event_id)
        while len(self._recoverable_cancel_event_ids) > 16:
            self._recoverable_cancel_event_ids.pop()

    def _begin_fence(
        self,
        *,
        response_id: str | None,
        cancel_event_id: str | None,
        clear_event_id: str | None,
        control_complete: bool,
    ) -> None:
        """Mute immediately and retain the interruption until both fences hold."""
        self._muted = True
        # A cached quiet observation from before the interruption cannot prove
        # that in-flight RTP belonging to the retired output has drained. Start
        # a fresh full quiescence window at the fence and let every decoded
        # frame reset it before playback can be unmuted.
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
        self._fence = _MediaFence(
            response_id=response_id,
            cancel_event_id=cancel_event_id,
            clear_event_id=clear_event_id,
            control_complete=control_complete,
        )
        self._arm_fence_timeout()

    def _maybe_complete_fence(self) -> None:
        fence = self._fence
        if fence is None or not fence.control_complete or not self._receiver_quiet:
            return
        self._cancel_timer("_fence_timeout_timer")
        values: dict[str, str | int] = {
            "event_type": "interrupt.fenced",
            "generation": self._generation,
        }
        if fence.response_id is not None:
            values["response_id"] = fence.response_id
        self._safe_lifecycle(values)
        self._fence = None
        self._muted = False

    def _note_receiver_audio(self) -> None:
        """Reset the actual-RTP quiet detector for every decoded audio frame."""
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
        if self._fence is not None and self._fence_timeout_timer is None:
            self._arm_fence_timeout()

    def _mark_receiver_quiet(self, serial: int) -> None:
        if self._closed or serial != self._media_activity_serial:
            return
        self._media_quiet_timer = None
        self._receiver_quiet = True
        if self._media_generation_open:
            self._media_generation_open = False
            self._safe_lifecycle(
                {
                    "event_type": "media.quiet",
                    "generation": self._generation,
                }
            )
        self._maybe_complete_fence()

    def _arm_fence_timeout(self) -> None:
        if self._fence_timeout_timer is not None or self._closed:
            return
        loop = asyncio.get_running_loop()
        fence = self._fence
        self._fence_timeout_timer = loop.call_later(
            MEDIA_FENCE_TIMEOUT_SECONDS,
            self._fence_timed_out,
            fence,
        )

    def _fence_timed_out(self, fence: _MediaFence | None) -> None:
        self._fence_timeout_timer = None
        if self._closed or fence is None or self._fence is not fence:
            return
        self._safe_fatal("media_fence_timeout")

    def _cancel_timer(self, attribute: str) -> None:
        timer = getattr(self, attribute)
        if timer is not None:
            timer.cancel()
            setattr(self, attribute, None)

    async def _consume_remote_audio(self, track: Any) -> None:
        assert AudioResampler is not None
        resampler = AudioResampler(
            format="s16",
            layout="mono",
            rate=PLAYBACK_SAMPLE_RATE,
        )
        try:
            while True:
                frame = await track.recv()
                converted = resampler.resample(frame)
                frames = converted if isinstance(converted, list) else [converted]
                for output in frames:
                    if output is None:
                        continue
                    size = output.samples * PCM_SAMPLE_WIDTH
                    pcm = bytes(output.planes[0])[:size]
                    if not pcm:
                        continue
                    self._note_receiver_audio()
                    if self._muted:
                        continue
                    if not self._media_generation_open:
                        self._generation += 1
                        self._media_generation_open = True
                        self._safe_lifecycle(
                            {
                                "event_type": "media.started",
                                "generation": self._generation,
                            }
                        )
                    media_timestamp = self._media_timestamp(output)
                    packet = PlaybackAudio(
                        generation=self._generation,
                        sample_index=self._playback_sample_index,
                        media_timestamp=media_timestamp,
                        pcm=pcm,
                    )
                    self._emit_playback(packet)
                    self._playback_sample_index += output.samples
        except MediaStreamError:
            if not self._closed:
                raise PeerError("remote audio track ended") from None

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

    def _safe_lifecycle(self, values: dict[str, str | int]) -> None:
        try:
            self._emit_lifecycle(values)
        except Exception:  # noqa: BLE001 - callback boundary must fail closed.
            self._safe_fatal("lifecycle_output_failed")

    def _safe_state(self, state: str) -> None:
        try:
            self._emit_state(state)
        except Exception:  # noqa: BLE001 - callback boundary must fail closed.
            self._safe_fatal("state_output_failed")

    def _safe_fatal(self, code: str) -> None:
        with suppress(Exception):
            self._emit_fatal(code)
