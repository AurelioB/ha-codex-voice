"""Tests for the real PyAV-backed outbound WebRTC track."""

from __future__ import annotations

import asyncio
from fractions import Fraction
from types import SimpleNamespace
from typing import Any

import pytest

from bridge import webrtc
from bridge.errors import ProtocolError
from bridge.webrtc import RTP_FRAME_SAMPLES, RTP_SAMPLE_RATE, PcmAudioTrack, WebRtcPeer


def _queue_only_peer(*, audio_queue_size: int = 1) -> WebRtcPeer:
    peer = object.__new__(WebRtcPeer)
    peer.audio = asyncio.Queue(maxsize=audio_queue_size)
    peer.data_events = asyncio.Queue(maxsize=1)
    peer.closed = False
    peer._transport_failed = asyncio.Event()
    peer._transport_error = None
    return peer


def test_webrtc_peer_disables_blocking_public_stun_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finite speech requests must not wait on aiortc's public STUN probe."""
    observed: dict[str, object] = {}

    class FakePeerConnection:
        def __init__(self, *, configuration: object) -> None:
            observed["configuration"] = configuration

        def addTrack(self, track: object) -> None:
            del track

        def createDataChannel(self, label: str) -> object:
            del label

            class Channel:
                def on(self, event: str):
                    del event
                    return lambda handler: handler

            return Channel()

        def on(self, event: str):
            del event
            return lambda handler: handler

    monkeypatch.setattr(webrtc, "RTCPeerConnection", FakePeerConnection)
    peer = WebRtcPeer()

    configuration = observed["configuration"]
    assert configuration.iceServers == []
    peer.input_track.stop()


@pytest.mark.asyncio
async def test_pcm_track_emits_proven_codex_rtp_shape() -> None:
    """24 kHz bridge PCM becomes paced 48 kHz mono frames for aiortc."""
    track = PcmAudioTrack()
    try:
        sample = (1_234).to_bytes(2, "little", signed=True)
        track.feed(sample * 480)
        frame = await track.recv()
        assert frame.format.name == "s16"
        assert frame.layout.name == "mono"
        assert frame.sample_rate == RTP_SAMPLE_RATE == 48_000
        assert frame.samples == RTP_FRAME_SAMPLES == 960
        assert frame.pts == 0
        assert frame.time_base == Fraction(1, RTP_SAMPLE_RATE)
        pcm = bytes(frame.planes[0])[: RTP_FRAME_SAMPLES * 2]
        assert pcm[:8] == sample * 4
        await track.wait_drained(timeout=1)
    finally:
        track.stop()


def test_pcm_track_bounds_queued_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webrtc, "MAX_INPUT_BUFFER_BYTES", 4)
    track = PcmAudioTrack()
    try:
        track.feed(b"\x00\x00" * 2)
        with pytest.raises(ProtocolError, match="buffer exceeds"):
            track.feed(b"\x00\x00")
    finally:
        track.stop()


def test_pcm_track_supports_a_tighter_live_session_bound() -> None:
    track = PcmAudioTrack()
    try:
        track.set_maximum_buffer_milliseconds(20)
        track.feed(b"\x00\x00" * 480)
        with pytest.raises(ProtocolError, match="buffer exceeds 20 ms"):
            track.feed(b"\x00\x00")
    finally:
        track.stop()


def test_pcm_track_default_still_accepts_finite_stt_utterances() -> None:
    """The live-device cap must not truncate whole-buffer STT adapters."""
    track = PcmAudioTrack()
    try:
        track.feed(b"\x00\x00" * (webrtc.REALTIME_SAMPLE_RATE * 2))
    finally:
        track.stop()


def test_webrtc_data_channel_sends_provider_control_only_when_open() -> None:
    class Channel:
        readyState = "open"

        def __init__(self) -> None:
            self.sent: list[str | bytes] = []

        def send(self, value: str | bytes) -> None:
            self.sent.append(value)

    channel = Channel()
    peer = object.__new__(WebRtcPeer)
    peer.data_channel = channel
    peer.closed = False
    peer._transport_error = None

    peer.send_data_event('{"type":"response.cancel"}')

    assert channel.sent == ['{"type":"response.cancel"}']


def test_webrtc_data_channel_send_fails_closed_before_open() -> None:
    peer = object.__new__(WebRtcPeer)
    peer.data_channel = SimpleNamespace(readyState="connecting")
    peer.closed = False
    peer._transport_error = None

    with pytest.raises(ProtocolError, match="data channel is not open"):
        peer.send_data_event('{"type":"response.cancel"}')


@pytest.mark.asyncio
async def test_remote_audio_overflow_is_terminal_instead_of_dropping_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = SimpleNamespace(
        format=SimpleNamespace(name="s16"),
        layout=SimpleNamespace(name="mono"),
        sample_rate=24_000,
        samples=1,
        planes=[b"\x01\x00"],
    )

    class Track:
        async def recv(self) -> Any:
            return frame

    class Resampler:
        def __init__(self, **_: Any) -> None:
            pass

        def resample(self, value: Any) -> list[Any]:
            return [value]

    monkeypatch.setattr(webrtc, "AudioResampler", Resampler)
    peer = _queue_only_peer()

    await asyncio.wait_for(peer._consume_audio(Track()), timeout=1)

    with pytest.raises(ProtocolError, match="remote audio buffer overflow"):
        await peer.recv_audio(timeout=0.1)


@pytest.mark.asyncio
async def test_remote_audio_eof_wakes_waiting_consumers_with_error() -> None:
    class EndedTrack:
        async def recv(self) -> Any:
            raise webrtc.MediaStreamError

    peer = _queue_only_peer()
    receiver = asyncio.create_task(peer.recv_audio())
    await asyncio.sleep(0)

    await peer._consume_audio(EndedTrack())

    with pytest.raises(ProtocolError, match="remote audio transport ended"):
        await asyncio.wait_for(receiver, timeout=1)
