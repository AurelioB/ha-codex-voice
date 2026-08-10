"""Tests for the isolated ThirdReality device WebRTC endpoint."""

from __future__ import annotations

import asyncio
import json
import socket
import time
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from device.thirdreality.realtime_client.sidecar import (
    SidecarBackpressure,
    SidecarLayout,
    WebRtcSidecarClient,
)
from device.thirdreality.webrtc_sidecar import peer as peer_module
from device.thirdreality.webrtc_sidecar.peer import (
    MAX_CAPTURE_AGE_MILLISECONDS,
    MAX_CAPTURE_QUEUE_FRAMES,
    CaptureAudioTrack,
    DeviceWebRtcPeer,
    PeerBackpressure,
)
from device.thirdreality.webrtc_sidecar.protocol import (
    CaptureAudio,
    ControlMessage,
    PlaybackAudio,
    ProtocolError,
    decode_packet,
    encode_capture_audio,
    encode_control,
    encode_playback_audio,
    sanitize_provider_lifecycle,
)
from device.thirdreality.webrtc_sidecar.runtime import SidecarRuntime


class FakeProcess:
    def __init__(self) -> None:
        self.return_code: int | None = None
        self.waits: list[float | None] = []
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.return_code

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        self.return_code = 0
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_ipc_audio_packets_preserve_capture_and_playback_metadata() -> None:
    capture_pcm = b"\x01\x00" * 320
    capture = decode_packet(
        encode_capture_audio(
            capture_pcm,
            sample_index=4_800,
            capture_monotonic_ns=9_123_456_789,
        )
    )
    assert capture == CaptureAudio(
        sample_index=4_800,
        capture_monotonic_ns=9_123_456_789,
        pcm=capture_pcm,
    )

    playback = PlaybackAudio(
        generation=7,
        sample_index=12_000,
        media_timestamp=11_520,
        pcm=b"\x02\x00" * 480,
    )
    assert decode_packet(encode_playback_audio(playback)) == playback


@pytest.mark.parametrize(
    "packet",
    [
        b"",
        b"\xffbad",
        encode_control("connected") + b"trailing",
        encode_capture_audio(
            b"\x00\x00",
            sample_index=0,
            capture_monotonic_ns=1,
        )[:-1],
    ],
)
def test_ipc_rejects_malformed_packets(packet: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_packet(packet)


def test_provider_lifecycle_sanitizer_never_forwards_transcripts_or_arguments() -> None:
    lifecycle = sanitize_provider_lifecycle(
        json.dumps(
            {
                "type": "turn.created",
                "turn": {
                    "id": "turn_123",
                    "role": "assistant",
                    "transcript": "this must not cross IPC",
                },
                "item": {
                    "id": "item_456",
                    "content": [{"text": "also secret"}],
                },
                "response": {"id": "resp_789", "output": "secret"},
                "arguments": {"private": "secret"},
                "transcript": "secret",
            }
        )
    )

    assert lifecycle == {
        "event_type": "turn.created",
        "role": "assistant",
        "item_id": "item_456",
        "response_id": "resp_789",
        "turn_id": "turn_123",
    }
    assert "secret" not in json.dumps(lifecycle)


def test_provider_lifecycle_sanitizer_rejects_content_disguised_as_identifier() -> None:
    lifecycle = sanitize_provider_lifecycle(
        '{"type":"response.created","response_id":"contains spaces and text"}'
    )
    assert lifecycle == {"event_type": "response.created"}


def test_launcher_uses_isolated_interpreter_and_explicit_runtime_path(
    tmp_path: Path,
) -> None:
    observed: dict[str, Any] = {}
    process = FakeProcess()
    probe: socket.socket | None = None

    def socketpair() -> tuple[socket.socket, socket.socket]:
        nonlocal probe
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        probe = child.dup()
        return parent, child

    def popen(argv: list[str], **kwargs: Any) -> FakeProcess:
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return process

    layout = SidecarLayout(
        python_executable=tmp_path / "python3",
        runtime_root=tmp_path / "runtime",
        source_root=tmp_path / "source",
    )
    client = WebRtcSidecarClient.launch(
        layout=layout,
        path_validator=lambda path, _directory: path,
        popen=popen,
        socketpair=socketpair,
    )
    assert probe is not None
    probe.settimeout(1)
    try:
        argv = observed["argv"]
        assert argv[:3] == [str(layout.python_executable), "-I", "-S"]
        assert argv[3] == str(layout.entrypoint)
        assert argv[-2:] == ["--runtime-root", str(layout.runtime_root)]
        kwargs = observed["kwargs"]
        assert kwargs["shell"] is False
        assert kwargs["close_fds"] is True
        assert kwargs["cwd"] == "/"
        assert kwargs["pass_fds"] == (int(argv[5]),)
        assert kwargs["env"] == {
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        assert kwargs["user"] == 65_534
        assert kwargs["group"] == 65_534
        assert kwargs["extra_groups"] == ()
        assert kwargs["umask"] == 0o077
        assert kwargs["start_new_session"] is True

        client.request_offer()
        assert decode_packet(probe.recv(4096)) == ControlMessage(
            type="create_offer",
            values={},
        )
        probe.send(encode_control("offer", sdp="v=0\r\n"))
        assert client.drain_messages() == [
            ControlMessage(type="offer", values={"sdp": "v=0\r\n"})
        ]
    finally:
        client.close()
        probe.close()
    assert len(process.waits) == 1
    assert process.waits[0] is not None
    assert 0 < process.waits[0] <= 0.5
    assert not process.terminated
    assert not process.killed


def test_launcher_audio_send_is_nonblocking_and_fails_on_pressure() -> None:
    class BackpressuredSocket:
        def setblocking(self, value: bool) -> None:
            assert value is False

        def send(self, packet: bytes) -> int:
            del packet
            raise BlockingIOError

        def fileno(self) -> int:
            return 10

        def close(self) -> None:
            pass

    client = WebRtcSidecarClient(BackpressuredSocket(), FakeProcess())  # type: ignore[arg-type]
    with pytest.raises(SidecarBackpressure):
        client.send_audio(
            b"\x00\x00" * 320,
            sample_index=0,
            capture_monotonic_ns=1,
        )


class FakePeer:
    def __init__(
        self,
        *,
        emit_lifecycle: Any,
        emit_playback: Any,
        emit_state: Any,
        emit_fatal: Any,
    ) -> None:
        self.emit_lifecycle = emit_lifecycle
        self.emit_playback = emit_playback
        self.emit_state = emit_state
        self.emit_fatal = emit_fatal
        self.captures: list[CaptureAudio] = []
        self.answers: list[str] = []
        self.cancellations: list[str | None] = []
        self.interruptions: list[str | None] = []
        self.stop_count = 0

    async def create_offer(self) -> str:
        return "v=0\r\na=fake-offer\r\n"

    async def set_answer(self, sdp: str) -> None:
        self.answers.append(sdp)
        self.emit_state("connected")
        self.emit_state("data.ready")

    def feed_capture(self, value: CaptureAudio) -> None:
        self.captures.append(value)

    def cancel_response(self, response_id: str | None = None) -> None:
        self.cancellations.append(response_id)

    def interrupt_response(self, response_id: str | None = None) -> None:
        self.interruptions.append(response_id)

    async def stop(self) -> None:
        self.stop_count += 1


async def _recv_packet(transport: socket.socket) -> Any:
    value = await asyncio.get_running_loop().sock_recv(transport, 65_537)
    return decode_packet(value)


@pytest.mark.asyncio
async def test_runtime_drives_offer_answer_audio_cancel_and_clean_shutdown() -> None:
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent.setblocking(False)
    peers: list[FakePeer] = []

    def peer_factory(**callbacks: Any) -> FakePeer:
        peer = FakePeer(**callbacks)
        peers.append(peer)
        return peer

    runtime = SidecarRuntime(child, peer_factory=peer_factory)
    run_task = asyncio.create_task(runtime.run())
    try:
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("create_offer"),
        )
        assert await _recv_packet(parent) == ControlMessage(
            type="offer",
            values={"sdp": "v=0\r\na=fake-offer\r\n"},
        )

        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("set_answer", sdp="v=0\r\na=fake-answer\r\n"),
        )
        answer_events = [await _recv_packet(parent) for _ in range(3)]
        assert answer_events == [
            ControlMessage(type="connected", values={}),
            ControlMessage(type="data.ready", values={}),
            ControlMessage(type="answer.applied", values={}),
        ]

        capture = CaptureAudio(
            sample_index=320,
            capture_monotonic_ns=4_000_000,
            pcm=b"\x01\x00" * 320,
        )
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_capture_audio(
                capture.pcm,
                sample_index=capture.sample_index,
                capture_monotonic_ns=capture.capture_monotonic_ns,
            ),
        )
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("response.interrupt", response_id="resp_123"),
        )
        async with asyncio.timeout(1):
            while not peers[0].captures or not peers[0].interruptions:
                await asyncio.sleep(0)
        assert peers[0].captures == [capture]
        assert peers[0].interruptions == ["resp_123"]

        playback = PlaybackAudio(
            generation=2,
            sample_index=480,
            media_timestamp=480,
            pcm=b"\x02\x00" * 480,
        )
        peers[0].emit_playback(playback)
        peers[0].emit_lifecycle(
            {
                "event_type": "response.cancelled",
                "response_id": "resp_123",
                "generation": 2,
            }
        )
        assert await _recv_packet(parent) == playback
        assert await _recv_packet(parent) == ControlMessage(
            type="lifecycle",
            values={
                "event_type": "response.cancelled",
                "response_id": "resp_123",
                "generation": 2,
            },
        )

        await asyncio.get_running_loop().sock_sendall(parent, encode_control("stop"))
        assert await _recv_packet(parent) == ControlMessage(type="stopped", values={})
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("shutdown"),
        )
        assert await _recv_packet(parent) == ControlMessage(
            type="shutdown.complete",
            values={},
        )
        assert await asyncio.wait_for(run_task, timeout=1) == 0
        assert peers[0].stop_count >= 1
    finally:
        parent.close()
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_runtime_fails_closed_when_audio_arrives_before_offer() -> None:
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent.setblocking(False)
    runtime = SidecarRuntime(child, peer_factory=FakePeer)
    run_task = asyncio.create_task(runtime.run())
    try:
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_capture_audio(
                b"\x00\x00" * 320,
                sample_index=0,
                capture_monotonic_ns=1,
            ),
        )
        assert await _recv_packet(parent) == ControlMessage(
            type="error",
            values={"code": "capture_outside_session"},
        )
        assert await asyncio.wait_for(run_task, timeout=1) == 1
    finally:
        parent.close()
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_runtime_bounds_cold_start_audio_in_track_before_answer() -> None:
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent.setblocking(False)
    peers: list[FakePeer] = []

    def peer_factory(**callbacks: Any) -> FakePeer:
        peer = FakePeer(**callbacks)
        peers.append(peer)
        return peer

    runtime = SidecarRuntime(child, peer_factory=peer_factory)
    run_task = asyncio.create_task(runtime.run())
    try:
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("create_offer"),
        )
        assert isinstance(await _recv_packet(parent), ControlMessage)
        capture = CaptureAudio(
            sample_index=0,
            capture_monotonic_ns=1,
            pcm=b"\x01\x00" * 320,
        )
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_capture_audio(
                capture.pcm,
                sample_index=0,
                capture_monotonic_ns=1,
            ),
        )
        async with asyncio.timeout(1):
            while not peers[0].captures:
                await asyncio.sleep(0)
        assert peers[0].captures == [capture]
        assert peers[0].answers == []
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("shutdown"),
        )
        assert await _recv_packet(parent) == ControlMessage(
            type="shutdown.complete",
            values={},
        )
        assert await asyncio.wait_for(run_task, timeout=1) == 0
    finally:
        parent.close()
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_capture_track_preserves_sample_clock_without_second_realtime_pacer() -> (
    None
):
    track = CaptureAudioTrack()
    captured_at = time.monotonic_ns()
    try:
        track.feed(
            CaptureAudio(
                sample_index=1_000,
                capture_monotonic_ns=captured_at,
                pcm=b"\x01\x00" * 320,
            )
        )
        track.feed(
            CaptureAudio(
                sample_index=1_320,
                capture_monotonic_ns=captured_at + 20_000_000,
                pcm=b"\x02\x00" * 320,
            )
        )
        first, second = await asyncio.gather(track.recv(), track.recv())
        assert first.sample_rate == 16_000
        assert first.samples == 320
        assert first.pts == 0
        assert second.pts == 320
        assert first.time_base == second.time_base == Fraction(1, 16_000)
    finally:
        track.stop()


def test_capture_track_queue_is_bounded_and_never_drops_silently() -> None:
    track = CaptureAudioTrack()
    captured_at = time.monotonic_ns()
    try:
        for index in range(MAX_CAPTURE_QUEUE_FRAMES):
            track.feed(
                CaptureAudio(
                    sample_index=index,
                    capture_monotonic_ns=captured_at + index,
                    pcm=b"\x00\x00",
                )
            )
        with pytest.raises(PeerBackpressure):
            track.feed(
                CaptureAudio(
                    sample_index=MAX_CAPTURE_QUEUE_FRAMES,
                    capture_monotonic_ns=(captured_at + MAX_CAPTURE_QUEUE_FRAMES),
                    pcm=b"\x00\x00",
                )
            )
    finally:
        track.stop()


def test_capture_track_rejects_stale_audio_instead_of_replaying_it_late() -> None:
    track = CaptureAudioTrack()
    try:
        stale_at = time.monotonic_ns() - (MAX_CAPTURE_AGE_MILLISECONDS + 1) * 1_000_000
        with pytest.raises(PeerBackpressure, match="age bound"):
            track.feed(
                CaptureAudio(
                    sample_index=0,
                    capture_monotonic_ns=stale_at,
                    pcm=b"\x00\x00" * 320,
                )
            )
    finally:
        track.stop()


class FakeChannel:
    readyState = "open"

    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.sent: list[str] = []

    def on(self, event: str) -> Any:
        def decorate(handler: Any) -> Any:
            self.handlers[event] = handler
            return handler

        return decorate

    def send(self, value: str) -> None:
        self.sent.append(value)


class FakePeerConnection:
    def __init__(self, *, configuration: Any) -> None:
        self.configuration = configuration
        self.handlers: dict[str, Any] = {}
        self.transceiver: tuple[Any, str] | None = None
        self.channel: FakeChannel | None = None
        self.iceGatheringState = "complete"
        self.connectionState = "new"
        self.localDescription = SimpleNamespace(sdp="v=0\r\na=device\r\n")

    def on(self, event: str) -> Any:
        def decorate(handler: Any) -> Any:
            self.handlers[event] = handler
            return handler

        return decorate

    def addTransceiver(self, track: Any, *, direction: str) -> None:
        self.transceiver = (track, direction)

    def createDataChannel(self, label: str, *, ordered: bool) -> FakeChannel:
        assert label == "oai-events"
        assert ordered is True
        self.channel = FakeChannel()
        return self.channel

    async def createOffer(self) -> object:
        return object()

    async def setLocalDescription(self, offer: object) -> None:
        del offer

    async def setRemoteDescription(self, answer: object) -> None:
        self.answer = answer

    async def close(self) -> None:
        self.connectionState = "closed"


def _fake_device_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[DeviceWebRtcPeer, list[Any]]:
    emitted: list[Any] = []

    class Configuration:
        def __init__(self, *, iceServers: list[Any]) -> None:
            self.iceServers = iceServers

    monkeypatch.setattr(peer_module, "RTCConfiguration", Configuration)
    monkeypatch.setattr(peer_module, "RTCPeerConnection", FakePeerConnection)
    peer = DeviceWebRtcPeer(
        emit_lifecycle=lambda value: emitted.append(("lifecycle", value)),
        emit_playback=lambda value: emitted.append(("playback", value)),
        emit_state=lambda value: emitted.append(("state", value)),
        emit_fatal=lambda value: emitted.append(("fatal", value)),
    )
    return peer, emitted


def test_peer_creates_audio_sendrecv_and_ordered_oai_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer, _emitted = _fake_device_peer(monkeypatch)
    try:
        assert peer.pc.configuration.iceServers == []
        assert peer.pc.transceiver == (peer.input_track, "sendrecv")
        assert peer.pc.channel is peer.data_channel
    finally:
        peer.input_track.stop()


class PassthroughResampler:
    def __init__(self, **kwargs: Any) -> None:
        assert kwargs == {"format": "s16", "layout": "mono", "rate": 24_000}

    def resample(self, frame: object) -> list[object]:
        return [frame]


class QueuedRemoteTrack:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[object] = asyncio.Queue()
        self.received = 0

    async def recv(self) -> object:
        value = await self.frames.get()
        self.received += 1
        return value


def _remote_frame(value: int, *, pts: int) -> object:
    return SimpleNamespace(
        samples=480,
        planes=[bytes((value, 0)) * 480],
        pts=pts,
        time_base=Fraction(1, 24_000),
    )


def _provider_event(peer: DeviceWebRtcPeer, **values: Any) -> None:
    peer.data_channel.handlers["message"](json.dumps(values))


async def _wait_for(predicate: Any) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


async def _close_audio_peer(
    peer: DeviceWebRtcPeer,
    task: asyncio.Task[None],
) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await peer.stop()


def test_peer_sanitizes_fixed_response_status_and_causal_error_id() -> None:
    assert sanitize_provider_lifecycle(
        json.dumps(
            {
                "type": "response.done",
                "response": {
                    "id": "resp_123",
                    "status": "completed",
                    "output": [{"transcript": "private"}],
                },
            }
        )
    ) == {
        "event_type": "response.done",
        "response_id": "resp_123",
        "response_status": "completed",
    }
    assert sanitize_provider_lifecycle(
        json.dumps(
            {
                "type": "error",
                "error": {
                    "event_id": "codex_device_cancel_1",
                    "type": "invalid_request_error",
                    "message": "private provider details",
                },
            }
        )
    ) == {
        "event_type": "error",
        "error_event_id": "codex_device_cancel_1",
    }
    assert sanitize_provider_lifecycle(
        '{"type":"response.done","response":{"status":"private text"}}'
    ) == {"event_type": "response.done"}


@pytest.mark.asyncio
async def test_peer_preserves_rtp_before_any_provider_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        pcm = b"\x03\x00" * 480
        track.frames.put_nowait(_remote_frame(3, pts=960))
        await _wait_for(lambda: any(kind == "playback" for kind, _ in emitted))
        assert emitted[:2] == [
            ("lifecycle", {"event_type": "media.started", "generation": 1}),
            (
                "playback",
                PlaybackAudio(
                    generation=1,
                    sample_index=0,
                    media_timestamp=960,
                    pcm=pcm,
                ),
            ),
        ]

        _provider_event(
            peer,
            type="response.created",
            response={"id": "resp_1", "status": "in_progress"},
        )
        assert emitted[-1][1]["generation"] == 1
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_interrupts_rtp_before_provider_start_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    monkeypatch.setattr(peer_module, "MEDIA_QUIET_SECONDS", 0.01)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        track.frames.put_nowait(_remote_frame(18, pts=0))
        await _wait_for(lambda: track.received == 1)
        emitted.clear()

        peer.interrupt_response("stale_parent_response")
        controls = [json.loads(value) for value in peer.data_channel.sent]
        assert [value["type"] for value in controls] == [
            "response.cancel",
            "output_audio_buffer.clear",
        ]
        assert "response_id" not in controls[0]

        track.frames.put_nowait(_remote_frame(19, pts=480))
        await _wait_for(lambda: track.received == 2)
        assert not any(kind == "playback" for kind, _ in emitted)
        _provider_event(
            peer,
            type="output_audio_buffer.cleared",
            response_id="actual_response",
        )
        await _wait_for(
            lambda: any(
                kind == "lifecycle" and value["event_type"] == "interrupt.fenced"
                for kind, value in emitted
            )
        )
        assert not any(kind == "fatal" for kind, _ in emitted)
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_retains_rtp_tail_after_provider_output_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        _provider_event(peer, type="output_audio_buffer.started", response_id="resp_1")
        track.frames.put_nowait(_remote_frame(4, pts=0))
        await _wait_for(lambda: track.received == 1)
        _provider_event(peer, type="output_audio_buffer.stopped", response_id="resp_1")
        track.frames.put_nowait(_remote_frame(5, pts=480))
        await _wait_for(lambda: track.received == 2)

        playback = [value for kind, value in emitted if kind == "playback"]
        assert [(value.generation, value.sample_index) for value in playback] == [
            (1, 0),
            (1, 480),
        ]
        assert playback[-1].pcm == b"\x05\x00" * 480
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_interrupts_post_stopped_rtp_tail_without_invalid_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    monkeypatch.setattr(peer_module, "MEDIA_QUIET_SECONDS", 0.01)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        _provider_event(peer, type="output_audio_buffer.started", response_id="resp_1")
        _provider_event(peer, type="output_audio_buffer.stopped", response_id="resp_1")
        track.frames.put_nowait(_remote_frame(20, pts=0))
        await _wait_for(lambda: track.received == 1)
        emitted.clear()

        peer.interrupt_response("resp_1")
        assert peer.data_channel.sent == []
        await _wait_for(
            lambda: any(
                kind == "lifecycle" and value["event_type"] == "interrupt.fenced"
                for kind, value in emitted
            )
        )
        assert not any(kind == "fatal" for kind, _ in emitted)
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_new_response_start_does_not_relabel_old_rtp_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        track.frames.put_nowait(_remote_frame(6, pts=0))
        await _wait_for(lambda: track.received == 1)
        _provider_event(
            peer,
            type="response.created",
            response={"id": "resp_2", "status": "in_progress"},
        )
        _provider_event(peer, type="output_audio_buffer.started", response_id="resp_2")
        track.frames.put_nowait(_remote_frame(7, pts=480))
        await _wait_for(lambda: track.received == 2)

        playback = [value for kind, value in emitted if kind == "playback"]
        assert [value.generation for value in playback] == [1, 1]
        assert [value.sample_index for value in playback] == [0, 480]
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_receiver_quiet_gap_starts_next_media_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    monkeypatch.setattr(peer_module, "MEDIA_QUIET_SECONDS", 0.01)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        track.frames.put_nowait(_remote_frame(8, pts=0))
        await _wait_for(
            lambda: any(
                kind == "lifecycle" and value["event_type"] == "media.quiet"
                for kind, value in emitted
            )
        )
        track.frames.put_nowait(_remote_frame(9, pts=480))
        await _wait_for(lambda: track.received == 2)

        lifecycle = [
            (value["event_type"], value["generation"])
            for kind, value in emitted
            if kind == "lifecycle" and value["event_type"].startswith("media.")
        ]
        assert lifecycle == [
            ("media.started", 1),
            ("media.quiet", 1),
            ("media.started", 2),
        ]
        playback = [value for kind, value in emitted if kind == "playback"]
        assert [(value.generation, value.sample_index) for value in playback] == [
            (1, 0),
            (2, 480),
        ]
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_explicit_interrupt_waits_for_clear_and_receiver_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    monkeypatch.setattr(peer_module, "MEDIA_QUIET_SECONDS", 0.01)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        _provider_event(
            peer,
            type="response.created",
            response={"id": "resp_1", "status": "in_progress"},
        )
        _provider_event(peer, type="output_audio_buffer.started", response_id="resp_1")
        track.frames.put_nowait(_remote_frame(10, pts=0))
        await _wait_for(lambda: track.received == 1)
        emitted.clear()

        peer.interrupt_response("resp_1")
        controls = [json.loads(value) for value in peer.data_channel.sent]
        assert [value["type"] for value in controls] == [
            "response.cancel",
            "output_audio_buffer.clear",
        ]
        assert controls[0]["response_id"] == "resp_1"
        assert controls[0]["event_id"] != controls[1]["event_id"]
        assert all(" " not in value["event_id"] for value in controls)

        track.frames.put_nowait(_remote_frame(11, pts=480))
        await _wait_for(lambda: track.received == 2)
        assert not any(kind == "playback" for kind, _ in emitted)
        _provider_event(peer, type="output_audio_buffer.cleared", response_id="resp_1")
        await _wait_for(
            lambda: any(
                kind == "lifecycle" and value["event_type"] == "interrupt.fenced"
                for kind, value in emitted
            )
        )
        track.frames.put_nowait(_remote_frame(12, pts=960))
        await _wait_for(lambda: track.received == 3)

        ordered = [
            value["event_type"] if kind == "lifecycle" else "playback"
            for kind, value in emitted
        ]
        assert ordered == [
            "output_audio_buffer.cleared",
            "media.quiet",
            "interrupt.fenced",
            "media.started",
            "playback",
        ]
        playback = [value for kind, value in emitted if kind == "playback"]
        assert playback == [
            PlaybackAudio(
                generation=2,
                sample_index=480,
                media_timestamp=960,
                pcm=b"\x0c\x00" * 480,
            )
        ]
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_completed_response_interrupt_sends_output_clear_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "MEDIA_QUIET_SECONDS", 0.01)
    peer, emitted = _fake_device_peer(monkeypatch)
    try:
        _provider_event(
            peer,
            type="response.created",
            response={"id": "resp_1", "status": "in_progress"},
        )
        _provider_event(peer, type="output_audio_buffer.started", response_id="resp_1")
        _provider_event(
            peer,
            type="response.done",
            response={"id": "resp_1", "status": "completed"},
        )
        emitted.clear()

        peer.interrupt_response("resp_1")
        assert [json.loads(value)["type"] for value in peer.data_channel.sent] == [
            "output_audio_buffer.clear"
        ]
        _provider_event(peer, type="output_audio_buffer.cleared", response_id="resp_1")
        await _wait_for(
            lambda: any(
                kind == "lifecycle" and value["event_type"] == "interrupt.fenced"
                for kind, value in emitted
            )
        )
        assert [
            value["event_type"] for kind, value in emitted if kind == "lifecycle"
        ] == [
            "output_audio_buffer.cleared",
            "interrupt.fenced",
        ]
    finally:
        await peer.stop()


@pytest.mark.parametrize("error_type", ["error", "invalid_request_error"])
@pytest.mark.asyncio
async def test_peer_cancel_noop_error_is_recoverable_and_connection_stays_live(
    monkeypatch: pytest.MonkeyPatch,
    error_type: str,
) -> None:
    monkeypatch.setattr(peer_module, "MEDIA_QUIET_SECONDS", 0.01)
    peer, emitted = _fake_device_peer(monkeypatch)
    try:
        _provider_event(
            peer,
            type="response.created",
            response={"id": "resp_1", "status": "in_progress"},
        )
        emitted.clear()
        peer.interrupt_response("resp_1")
        cancel = json.loads(peer.data_channel.sent[-1])
        assert cancel["type"] == "response.cancel"

        _provider_event(
            peer,
            type=error_type,
            error={
                "event_id": cancel["event_id"],
                "message": "private cancel details",
            },
        )
        await _wait_for(
            lambda: any(
                kind == "lifecycle" and value["event_type"] == "interrupt.fenced"
                for kind, value in emitted
            )
        )
        _provider_event(peer, type="session.updated")
        assert not any(kind == "fatal" for kind, _ in emitted)
        assert [
            value["event_type"] for kind, value in emitted if kind == "lifecycle"
        ] == [
            "interrupt.fenced",
            "session.updated",
        ]
        assert "private" not in json.dumps(emitted)
    finally:
        await peer.stop()


@pytest.mark.asyncio
async def test_peer_clear_correlated_error_is_content_free_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer, emitted = _fake_device_peer(monkeypatch)
    try:
        _provider_event(peer, type="output_audio_buffer.started", response_id="resp_1")
        emitted.clear()
        peer.interrupt_response("resp_1")
        clear = json.loads(peer.data_channel.sent[-1])

        _provider_event(
            peer,
            type="server_error",
            error={
                "event_id": clear["event_id"],
                "message": "private clear details",
            },
        )
        assert emitted == [("fatal", "output_clear_failed")]
        assert "private" not in json.dumps(emitted)
    finally:
        await peer.stop()


@pytest.mark.asyncio
async def test_peer_provider_speech_started_fences_without_client_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    monkeypatch.setattr(peer_module, "MEDIA_QUIET_SECONDS", 0.01)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        track.frames.put_nowait(_remote_frame(13, pts=0))
        await _wait_for(lambda: track.received == 1)
        emitted.clear()
        _provider_event(peer, type="input_audio_buffer.speech_started")
        assert peer.data_channel.sent == []

        track.frames.put_nowait(_remote_frame(14, pts=480))
        await _wait_for(
            lambda: any(
                kind == "lifecycle" and value["event_type"] == "interrupt.fenced"
                for kind, value in emitted
            )
        )
        assert [
            value["event_type"] for kind, value in emitted if kind == "lifecycle"
        ] == [
            "input_audio_buffer.speech_started",
            "media.quiet",
            "interrupt.fenced",
        ]
        assert not any(kind == "playback" for kind, _ in emitted)
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_interrupt_without_receiver_gap_fails_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    monkeypatch.setattr(peer_module, "MEDIA_QUIET_SECONDS", 0.02)
    monkeypatch.setattr(peer_module, "MEDIA_FENCE_TIMEOUT_SECONDS", 0.05)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        track.frames.put_nowait(_remote_frame(15, pts=0))
        await _wait_for(lambda: track.received == 1)
        peer.interrupt_response()

        deadline = asyncio.get_running_loop().time() + 0.08
        pts = 480
        while asyncio.get_running_loop().time() < deadline:
            track.frames.put_nowait(_remote_frame(15, pts=pts))
            pts += 480
            await asyncio.sleep(0.005)
        await _wait_for(lambda: ("fatal", "media_fence_timeout") in emitted)
        assert ("fatal", "media_fence_timeout") in emitted
        assert not any(
            kind == "lifecycle" and value["event_type"] == "interrupt.fenced"
            for kind, value in emitted
        )
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_interrupt_without_control_settlement_fails_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "MEDIA_FENCE_TIMEOUT_SECONDS", 0.01)
    peer, emitted = _fake_device_peer(monkeypatch)
    try:
        _provider_event(
            peer,
            type="response.created",
            response={"id": "resp_1", "status": "in_progress"},
        )
        emitted.clear()
        peer.interrupt_response("resp_1")

        await _wait_for(lambda: ("fatal", "media_fence_timeout") in emitted)
        assert emitted == [("fatal", "media_fence_timeout")]
    finally:
        await peer.stop()


@pytest.mark.asyncio
async def test_peer_interrupt_requires_fresh_post_fence_receiver_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    monkeypatch.setattr(peer_module, "MEDIA_QUIET_SECONDS", 0.02)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        peer.interrupt_response()
        assert not any(
            kind == "lifecycle" and value["event_type"] == "interrupt.fenced"
            for kind, value in emitted
        )

        # A late packet from the retired output arrives during the new quiet
        # proof window. It is muted and restarts the complete interval.
        await asyncio.sleep(0.01)
        track.frames.put_nowait(_remote_frame(16, pts=0))
        await _wait_for(lambda: track.received == 1)
        await asyncio.sleep(0.015)
        assert not any(
            kind == "lifecycle" and value["event_type"] == "interrupt.fenced"
            for kind, value in emitted
        )
        await _wait_for(
            lambda: any(
                kind == "lifecycle" and value["event_type"] == "interrupt.fenced"
                for kind, value in emitted
            )
        )
        assert not any(kind == "playback" for kind, _ in emitted)

        track.frames.put_nowait(_remote_frame(17, pts=480))
        await _wait_for(lambda: track.received == 2)
        assert [
            value["event_type"] if kind == "lifecycle" else "playback"
            for kind, value in emitted
        ] == ["interrupt.fenced", "media.started", "playback"]
    finally:
        await _close_audio_peer(peer, task)
