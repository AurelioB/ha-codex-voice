"""Tests for the real PyAV-backed outbound WebRTC track."""

from __future__ import annotations

import asyncio
from collections import deque
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
    peer._audio_replay = deque()
    peer.data_events = asyncio.Queue(maxsize=1)
    peer._data_event_replay = deque()
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


@pytest.mark.asyncio
async def test_cancelled_audio_receive_replays_won_item_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation cannot erase PCM already removed by the inner queue task."""
    peer = _queue_only_peer(audio_queue_size=2)
    first = b"first-audio"
    second = b"second-audio"
    peer.audio.put_nowait(first)
    original_wait = asyncio.wait
    inner_receive_won = asyncio.Event()
    release_wrapper = asyncio.Event()

    async def pause_after_inner_receive(*args: Any, **kwargs: Any) -> Any:
        result = await original_wait(*args, **kwargs)
        inner_receive_won.set()
        await release_wrapper.wait()
        return result

    monkeypatch.setattr(webrtc.asyncio, "wait", pause_after_inner_receive)
    receiver = asyncio.create_task(peer.recv_audio())
    await asyncio.wait_for(inner_receive_won.wait(), timeout=1)
    assert peer.audio.empty()
    peer.audio.put_nowait(second)

    receiver.cancel()
    with pytest.raises(asyncio.CancelledError):
        await receiver

    assert peer.drain_audio_nowait() == [first, second]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["audio", "data"])
async def test_cancelled_receive_during_cleanup_replays_once_in_order(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Cancellation in child cleanup cannot erase or duplicate a won item."""
    peer = _queue_only_peer(audio_queue_size=2)
    if kind == "audio":
        queue = peer.audio
        receive = peer.recv_audio
        drain = peer.drain_audio_nowait
        first: str | bytes = b"first-audio"
        second: str | bytes = b"second-audio"
    else:
        peer.data_events = asyncio.Queue(maxsize=2)
        queue = peer.data_events
        receive = peer.recv_data_event
        drain = peer.drain_data_events_nowait
        first = "first-data"
        second = "second-data"
    queue.put_nowait(first)
    original_gather = asyncio.gather
    cleanup_started = asyncio.Event()
    pause_cleanup = True

    async def pause_first_cleanup(*args: Any, **kwargs: Any) -> Any:
        nonlocal pause_cleanup
        result = await original_gather(*args, **kwargs)
        if pause_cleanup:
            pause_cleanup = False
            cleanup_started.set()
            await asyncio.Future()
        return result

    monkeypatch.setattr(webrtc.asyncio, "gather", pause_first_cleanup)
    receiver = asyncio.create_task(receive())
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    assert queue.empty()
    queue.put_nowait(second)

    receiver.cancel()
    with pytest.raises(asyncio.CancelledError):
        await receiver

    assert drain() == [first, second]
    assert drain() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["audio", "data"])
async def test_terminal_transport_error_precedes_cancelled_receive_replay(
    kind: str,
) -> None:
    """A terminal fault cannot expose PCM or data retained for cancellation."""
    peer = _queue_only_peer()
    if kind == "audio":
        replay = peer._audio_replay
        receive = peer.recv_audio
        drain = peer.drain_audio_nowait
        stale: str | bytes = b"stale-audio"
    else:
        replay = peer._data_event_replay
        receive = peer.recv_data_event
        drain = peer.drain_data_events_nowait
        stale = "stale-data"
    replay.append(stale)
    peer._fail_transport("terminal transport failure")

    with pytest.raises(ProtocolError, match="terminal transport failure"):
        await receive()
    assert list(replay) == [stale]

    with pytest.raises(ProtocolError, match="terminal transport failure"):
        drain()
    assert list(replay) == [stale]


def test_replay_counts_toward_transport_queue_bound() -> None:
    peer = _queue_only_peer(audio_queue_size=1)
    peer._audio_replay.append(b"replayed")

    accepted = peer._put_transport_item(
        peer.audio,
        peer._audio_replay,
        b"new",
        overflow_message="WebRTC remote audio buffer overflow",
    )

    assert not accepted
    with pytest.raises(ProtocolError, match="remote audio buffer overflow"):
        peer.drain_audio_nowait()
