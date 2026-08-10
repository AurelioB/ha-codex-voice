"""Minimal aiortc peer used by Codex app-server's subscription WebRTC path."""

from __future__ import annotations

import asyncio
import logging
from fractions import Fraction
from typing import Any

from .audio import PCM_SAMPLE_WIDTH, REALTIME_SAMPLE_RATE
from .errors import ProtocolError, WebRtcUnavailable

LOGGER = logging.getLogger(__name__)
FRAME_DURATION_MS = 20
RTP_SAMPLE_RATE = 48_000
INPUT_FRAME_SAMPLES = REALTIME_SAMPLE_RATE * FRAME_DURATION_MS // 1_000
INPUT_FRAME_BYTES = INPUT_FRAME_SAMPLES * PCM_SAMPLE_WIDTH
RTP_FRAME_SAMPLES = RTP_SAMPLE_RATE * FRAME_DURATION_MS // 1_000
MAX_INPUT_BUFFER_MILLISECONDS = 300_000
MAX_INPUT_BUFFER_BYTES = (
    REALTIME_SAMPLE_RATE * PCM_SAMPLE_WIDTH * MAX_INPUT_BUFFER_MILLISECONDS // 1_000
)
MAX_REMOTE_AUDIO_QUEUE_CHUNKS = 25
MAX_DATA_EVENT_QUEUE_ITEMS = 64

try:  # Imported lazily enough that text-only conversation can report a useful error.
    from aiortc import (
        MediaStreamTrack,
        RTCConfiguration,
        RTCPeerConnection,
        RTCSessionDescription,
    )
    from aiortc.mediastreams import MediaStreamError
    from av import AudioFrame
    from av.audio.resampler import AudioResampler
except (
    ImportError
) as import_error:  # pragma: no cover - exercised in dependency-free installs.
    MediaStreamTrack = object  # type: ignore[assignment,misc]
    RTCConfiguration = None  # type: ignore[assignment,misc]
    RTCPeerConnection = None  # type: ignore[assignment,misc]
    RTCSessionDescription = None  # type: ignore[assignment,misc]
    AudioFrame = None  # type: ignore[assignment,misc]
    AudioResampler = None  # type: ignore[assignment,misc]
    MediaStreamError = Exception  # type: ignore[assignment,misc]
    _IMPORT_ERROR: ImportError | None = import_error
else:
    _IMPORT_ERROR = None


class PcmAudioTrack(MediaStreamTrack):
    """A paced, queue-backed 24 kHz mono PCM16 WebRTC input track."""

    kind = "audio"

    def __init__(self) -> None:
        if _IMPORT_ERROR is not None:
            raise WebRtcUnavailable(
                "aiortc and av are required for audio endpoints"
            ) from _IMPORT_ERROR
        super().__init__()
        self._buffer = bytearray()
        self._maximum_buffer_milliseconds = MAX_INPUT_BUFFER_MILLISECONDS
        self._maximum_buffer_bytes = MAX_INPUT_BUFFER_BYTES
        self._pts = 0
        self._started_at: float | None = None
        self._drained = asyncio.Event()
        self._drained.set()

    def feed(self, pcm: bytes) -> None:
        if len(pcm) % PCM_SAMPLE_WIDTH:
            raise ProtocolError("outgoing PCM16 audio is not sample-aligned")
        if len(self._buffer) + len(pcm) > self._maximum_buffer_bytes:
            raise ProtocolError(
                f"outgoing audio buffer exceeds {self._maximum_buffer_milliseconds} ms"
            )
        if pcm:
            self._buffer.extend(pcm)
            self._drained.clear()

    def set_maximum_buffer_milliseconds(self, maximum: int) -> None:
        """Narrow the input bound before feeding a latency-sensitive session."""
        if maximum <= 0:
            raise ValueError("maximum input buffer duration must be positive")
        maximum_bytes = REALTIME_SAMPLE_RATE * PCM_SAMPLE_WIDTH * maximum // 1_000
        if len(self._buffer) > maximum_bytes:
            raise ProtocolError("queued audio exceeds the requested input bound")
        self._maximum_buffer_milliseconds = maximum
        self._maximum_buffer_bytes = maximum_bytes

    async def wait_drained(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self._drained.wait()
        else:
            await asyncio.wait_for(self._drained.wait(), timeout)

    def discard_pending(self) -> None:
        """Drop unsent input after finite STT has reached its final transcript."""
        self._buffer.clear()
        self._drained.set()

    async def recv(self) -> Any:
        loop = asyncio.get_running_loop()
        if self._started_at is None:
            self._started_at = loop.time()
        target = self._started_at + self._pts / RTP_SAMPLE_RATE
        delay = target - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)

        available = min(len(self._buffer), INPUT_FRAME_BYTES)
        chunk = bytes(self._buffer[:available])
        if available:
            del self._buffer[:available]
        if available < INPUT_FRAME_BYTES:
            chunk += bytes(INPUT_FRAME_BYTES - available)
        if not self._buffer:
            self._drained.set()

        assert AudioFrame is not None
        # The public bridge contract is 24 kHz PCM. The negotiated Opus track
        # is clocked at 48 kHz, so duplicate each PCM16 sample before handing
        # the frame to aiortc. This mirrors the proven Codex desktop WebRTC
        # shape (s16 mono, 960 samples every 20 ms).
        upsampled = b"".join(
            chunk[index : index + 2] * 2 for index in range(0, len(chunk), 2)
        )
        frame = AudioFrame(format="s16", layout="mono", samples=RTP_FRAME_SAMPLES)
        frame.planes[0].update(upsampled)
        frame.sample_rate = RTP_SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = Fraction(1, RTP_SAMPLE_RATE)
        self._pts += RTP_FRAME_SAMPLES
        return frame


class WebRtcPeer:
    """Create an SDP offer, carry microphone PCM, and expose received PCM."""

    def __init__(self) -> None:
        if (
            _IMPORT_ERROR is not None
            or RTCConfiguration is None
            or RTCPeerConnection is None
        ):
            raise WebRtcUnavailable(
                "aiortc and av are required for audio endpoints"
            ) from _IMPORT_ERROR
        # aiortc otherwise injects its public default STUN server. On hosts where
        # that UDP probe is filtered, gathering blocks for five seconds even
        # though host candidates connect to Codex successfully through ordinary
        # outbound ICE checks. Codex supplies the remote service candidates, so
        # do not make every finite STT and TTS request depend on third-party STUN.
        self.pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
        self.input_track = PcmAudioTrack()
        self.pc.addTrack(self.input_track)
        self.data_channel = self.pc.createDataChannel("oai-events")
        self.audio: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=MAX_REMOTE_AUDIO_QUEUE_CHUNKS
        )
        self.data_events: asyncio.Queue[str | bytes] = asyncio.Queue(
            maxsize=MAX_DATA_EVENT_QUEUE_ITEMS
        )
        self.connection_state = asyncio.Event()
        self.closed = False
        self._transport_failed = asyncio.Event()
        self._transport_error: ProtocolError | None = None
        self._consumer_tasks: set[asyncio.Task[None]] = set()

        @self.pc.on("track")
        def on_track(track: Any) -> None:
            if getattr(track, "kind", None) != "audio":
                return
            LOGGER.debug("Codex WebRTC remote audio track attached")
            task = asyncio.create_task(
                self._consume_audio(track), name="codex-webrtc-audio"
            )
            self._consumer_tasks.add(task)
            task.add_done_callback(self._audio_consumer_done)

        @self.pc.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            self.connection_state.set()
            if self.pc.connectionState == "failed" or (
                self.pc.connectionState == "closed" and not self.closed
            ):
                self._fail_transport(
                    f"WebRTC connection entered {self.pc.connectionState} state"
                )
                LOGGER.warning(
                    "Codex WebRTC connection state: %s", self.pc.connectionState
                )

        @self.data_channel.on("message")
        def on_message(message: str | bytes) -> None:
            try:
                self.data_events.put_nowait(message)
            except asyncio.QueueFull:
                self._fail_transport("WebRTC data-channel event buffer overflow")

        @self.data_channel.on("close")
        def on_data_channel_close() -> None:
            if not self.closed:
                self._fail_transport("WebRTC data channel ended unexpectedly")

    async def create_offer(self) -> str:
        gathering_complete = asyncio.Event()

        @self.pc.on("icegatheringstatechange")
        def on_icegatheringstatechange() -> None:
            if self.pc.iceGatheringState == "complete":
                gathering_complete.set()

        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        if self.pc.iceGatheringState != "complete":
            await asyncio.wait_for(gathering_complete.wait(), timeout=10)
        if self.pc.localDescription is None or not self.pc.localDescription.sdp:
            raise ProtocolError("WebRTC did not produce a local SDP offer")
        return self.pc.localDescription.sdp

    async def set_answer(self, sdp: str) -> None:
        if not sdp:
            raise ProtocolError("app-server returned an empty WebRTC SDP answer")
        assert RTCSessionDescription is not None
        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=sdp, type="answer")
        )

    async def wait_connected(self, timeout: float | None = None) -> None:
        async def wait() -> None:
            while self.pc.connectionState != "connected":
                observed_state = self.pc.connectionState
                if observed_state in {"failed", "closed"}:
                    raise ProtocolError(
                        f"WebRTC connection entered {observed_state} state"
                    )
                self.connection_state.clear()
                if self.pc.connectionState != observed_state:
                    continue
                await self.connection_state.wait()

        if timeout is None:
            await wait()
        else:
            await asyncio.wait_for(wait(), timeout)

    def set_input_buffer_limit(self, maximum_milliseconds: int) -> None:
        """Apply a per-session input bound to the paced microphone track."""
        self.input_track.set_maximum_buffer_milliseconds(maximum_milliseconds)

    def feed_audio(self, pcm: bytes) -> None:
        self.input_track.feed(pcm)

    async def wait_input_drained(self, timeout: float | None = None) -> None:
        await self.input_track.wait_drained(timeout)

    def discard_pending_input(self) -> None:
        """Prevent a finite STT input tail from entering a reused session."""
        self.input_track.discard_pending()

    def drain_audio_nowait(self) -> list[bytes]:
        """Remove and return already-buffered remote PCM."""
        chunks: list[bytes] = []
        while True:
            try:
                chunks.append(self.audio.get_nowait())
            except asyncio.QueueEmpty:
                return chunks

    def drain_data_events_nowait(self) -> list[str | bytes]:
        """Remove and return already-buffered data-channel events."""
        events: list[str | bytes] = []
        while True:
            try:
                events.append(self.data_events.get_nowait())
            except asyncio.QueueEmpty:
                return events

    async def recv_audio(self, timeout: float | None = None) -> bytes:
        value = await self._recv_transport_queue(self.audio, timeout)
        assert isinstance(value, bytes)
        return value

    async def recv_data_event(self, timeout: float | None = None) -> str | bytes:
        value = await self._recv_transport_queue(self.data_events, timeout)
        assert isinstance(value, (str, bytes))
        return value

    def send_data_event(self, value: str | bytes) -> None:
        """Send one bounded provider control over the negotiated data channel."""
        if not isinstance(value, (str, bytes)):
            raise ProtocolError("WebRTC data event must be text or bytes")
        if self.closed:
            raise ProtocolError("WebRTC data channel is closed")
        if self._transport_error is not None:
            raise self._transport_error
        if getattr(self.data_channel, "readyState", None) != "open":
            raise ProtocolError("WebRTC data channel is not open")
        try:
            self.data_channel.send(value)
        except Exception as exc:
            message = f"WebRTC data channel send failed: {exc}"
            self._fail_transport(message)
            raise ProtocolError(message) from exc

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.input_track.stop()
        await self.pc.close()
        for task in tuple(self._consumer_tasks):
            task.cancel()
        await asyncio.gather(*self._consumer_tasks, return_exceptions=True)
        self._consumer_tasks.clear()

    def _audio_consumer_done(self, task: asyncio.Task[None]) -> None:
        """Retire the remote audio task and surface transport failures."""
        self._consumer_tasks.discard(task)
        if task.cancelled():
            return
        if error := task.exception():
            self._fail_transport(f"WebRTC audio transport failed: {error}")
            LOGGER.error("Codex WebRTC audio consumer failed: %s", error)

    def _fail_transport(self, message: str) -> None:
        """Wake all media consumers with the first terminal transport fault."""
        if self.closed or self._transport_error is not None:
            return
        self._transport_error = ProtocolError(message)
        self._transport_failed.set()

    async def _recv_transport_queue(
        self,
        queue: asyncio.Queue[Any],
        timeout: float | None,
    ) -> Any:
        if self._transport_error is not None:
            raise self._transport_error
        item_task = asyncio.create_task(queue.get())
        failure_task = asyncio.create_task(self._transport_failed.wait())
        try:
            done, _ = await asyncio.wait(
                {item_task, failure_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError
            if failure_task in done:
                assert self._transport_error is not None
                raise self._transport_error
            return item_task.result()
        finally:
            for task in (item_task, failure_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(item_task, failure_task, return_exceptions=True)

    async def _consume_audio(self, track: Any) -> None:
        assert AudioResampler is not None
        resampler = AudioResampler(
            format="s16", layout="mono", rate=REALTIME_SAMPLE_RATE
        )
        received_frame = False
        logged_resample = False
        logged_queue = False
        try:
            while True:
                frame = await track.recv()
                if not received_frame:
                    LOGGER.debug(
                        "Codex WebRTC received its first remote audio frame "
                        "(format=%s layout=%s rate=%s samples=%s)",
                        frame.format.name,
                        frame.layout.name,
                        frame.sample_rate,
                        frame.samples,
                    )
                    received_frame = True
                output = resampler.resample(frame)
                frames = output if isinstance(output, list) else [output]
                if frames and not logged_resample:
                    LOGGER.debug(
                        "Codex WebRTC resampler produced %d frame(s)", len(frames)
                    )
                    logged_resample = True
                for converted in frames:
                    if converted is None:
                        continue
                    size = converted.samples * PCM_SAMPLE_WIDTH
                    pcm = bytes(converted.planes[0])[:size]
                    if not pcm:
                        continue
                    if self.audio.full():
                        self._fail_transport("WebRTC remote audio buffer overflow")
                        return
                    self.audio.put_nowait(pcm)
                    if not logged_queue:
                        LOGGER.debug(
                            "Codex WebRTC queued its first PCM chunk (%d bytes)",
                            len(pcm),
                        )
                        logged_queue = True
        except MediaStreamError as exc:
            if not self.closed:
                self._fail_transport("WebRTC remote audio transport ended")
                LOGGER.debug("Codex WebRTC audio track ended: %s", exc)
