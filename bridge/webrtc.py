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
MAX_INPUT_BUFFER_BYTES = REALTIME_SAMPLE_RATE * PCM_SAMPLE_WIDTH * 300

try:  # Imported lazily enough that text-only conversation can report a useful error.
    from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
    from aiortc.mediastreams import MediaStreamError
    from av import AudioFrame
    from av.audio.resampler import AudioResampler
except (
    ImportError
) as import_error:  # pragma: no cover - exercised in dependency-free installs.
    MediaStreamTrack = object  # type: ignore[assignment,misc]
    RTCPeerConnection = None  # type: ignore[assignment]
    RTCSessionDescription = None  # type: ignore[assignment]
    AudioFrame = None  # type: ignore[assignment]
    AudioResampler = None  # type: ignore[assignment]
    MediaStreamError = Exception  # type: ignore[assignment,misc]
    _IMPORT_ERROR: ImportError | None = import_error
else:
    _IMPORT_ERROR = None


class PcmAudioTrack(MediaStreamTrack):  # type: ignore[misc,valid-type]
    """A paced, queue-backed 24 kHz mono PCM16 WebRTC input track."""

    kind = "audio"

    def __init__(self) -> None:
        if _IMPORT_ERROR is not None:
            raise WebRtcUnavailable(
                "aiortc and av are required for audio endpoints"
            ) from _IMPORT_ERROR
        super().__init__()
        self._buffer = bytearray()
        self._pts = 0
        self._started_at: float | None = None
        self._drained = asyncio.Event()
        self._drained.set()

    def feed(self, pcm: bytes) -> None:
        if len(pcm) % PCM_SAMPLE_WIDTH:
            raise ProtocolError("outgoing PCM16 audio is not sample-aligned")
        if len(self._buffer) + len(pcm) > MAX_INPUT_BUFFER_BYTES:
            raise ProtocolError("outgoing audio buffer exceeds five minutes")
        if pcm:
            self._buffer.extend(pcm)
            self._drained.clear()

    async def wait_drained(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self._drained.wait()
        else:
            await asyncio.wait_for(self._drained.wait(), timeout)

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
        if _IMPORT_ERROR is not None or RTCPeerConnection is None:
            raise WebRtcUnavailable(
                "aiortc and av are required for audio endpoints"
            ) from _IMPORT_ERROR
        self.pc = RTCPeerConnection()
        self.input_track = PcmAudioTrack()
        self.pc.addTrack(self.input_track)
        self.data_channel = self.pc.createDataChannel("oai-events")
        self.audio: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2_048)
        self.data_events: asyncio.Queue[str | bytes] = asyncio.Queue(maxsize=128)
        self.connection_state = asyncio.Event()
        self.closed = False
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
                LOGGER.warning(
                    "Codex WebRTC connection state: %s", self.pc.connectionState
                )

        @self.data_channel.on("message")
        def on_message(message: str | bytes) -> None:
            try:
                self.data_events.put_nowait(message)
            except asyncio.QueueFull:
                LOGGER.warning("Dropping Codex WebRTC data-channel event")

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

    def feed_audio(self, pcm: bytes) -> None:
        self.input_track.feed(pcm)

    async def wait_input_drained(self, timeout: float | None = None) -> None:
        await self.input_track.wait_drained(timeout)

    async def recv_audio(self, timeout: float | None = None) -> bytes:
        if timeout is None:
            return await self.audio.get()
        return await asyncio.wait_for(self.audio.get(), timeout)

    async def recv_data_event(self, timeout: float | None = None) -> str | bytes:
        if timeout is None:
            return await self.data_events.get()
        return await asyncio.wait_for(self.data_events.get(), timeout)

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
            LOGGER.error("Codex WebRTC audio consumer failed: %s", error)

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
                        self.audio.get_nowait()
                    self.audio.put_nowait(pcm)
                    if not logged_queue:
                        LOGGER.debug(
                            "Codex WebRTC queued its first PCM chunk (%d bytes)",
                            len(pcm),
                        )
                        logged_queue = True
        except MediaStreamError as exc:
            if not self.closed:
                LOGGER.debug("Codex WebRTC audio track ended: %s", exc)
