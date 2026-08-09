"""Tests for the real PyAV-backed outbound WebRTC track."""

from __future__ import annotations

from fractions import Fraction

import pytest

from bridge import webrtc
from bridge.errors import ProtocolError
from bridge.webrtc import RTP_FRAME_SAMPLES, RTP_SAMPLE_RATE, PcmAudioTrack, WebRtcPeer


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
