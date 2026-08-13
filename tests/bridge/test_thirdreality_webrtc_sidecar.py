"""Tests for the isolated ThirdReality device WebRTC endpoint."""

from __future__ import annotations

import asyncio
import json
import queue as thread_queue
import socket
import struct
import subprocess
import threading
import time
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiortc.codecs.opus import OpusEncoder

from device.thirdreality.realtime_client.sidecar import (
    SidecarBackpressure,
    SidecarLayout,
    WebRtcSidecarClient,
)
from device.thirdreality.webrtc_sidecar import peer as peer_module
from device.thirdreality.webrtc_sidecar.peer import (
    MAX_CAPTURE_AGE_MILLISECONDS,
    MAX_CAPTURE_QUEUE_FRAMES,
    MAX_CAPTURE_QUEUE_MILLISECONDS,
    MAX_STARTUP_CAPTURE_AGE_MILLISECONDS,
    CaptureAudioTrack,
    DeviceWebRtcPeer,
    PeerBackpressure,
    PeerError,
    _apply_capture_gain_pcm16,
)
from device.thirdreality.webrtc_sidecar.protocol import (
    CONTROL_KIND,
    WIRE_VERSION,
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


def test_ipc_exposes_interrupt_but_not_unsupported_provider_cancel() -> None:
    assert decode_packet(encode_control("response.interrupt")) == ControlMessage(
        type="response.interrupt",
        values={},
    )
    with pytest.raises(ProtocolError):
        encode_control("response.interrupt", response_id="resp_1")
    with pytest.raises(ProtocolError):
        encode_control("response.cancel", response_id="resp_1")


@pytest.mark.parametrize("message_type", ["capture.commit", "capture.ready"])
def test_ipc_round_trips_fieldless_capture_barrier(message_type: str) -> None:
    assert decode_packet(encode_control(message_type)) == ControlMessage(
        type=message_type,
        values={},
    )
    with pytest.raises(ProtocolError, match="unexpected field"):
        encode_control(message_type, sample_end=320)


@pytest.mark.parametrize("gain_db", [0.0, 6.25, 18.0])
def test_ipc_round_trips_strict_capture_gain(gain_db: float) -> None:
    assert decode_packet(
        encode_control("create_offer", direct_capture_gain_db=gain_db)
    ) == ControlMessage(
        type="create_offer",
        values={"direct_capture_gain_db": gain_db},
    )


def test_ipc_round_trips_epoch_tagged_standby_controls() -> None:
    assert decode_packet(
        encode_control("standby.create_offer", direct_capture_gain_db=6.0)
    ) == ControlMessage(
        type="standby.create_offer",
        values={"direct_capture_gain_db": 6.0},
    )
    assert decode_packet(
        encode_control("standby.offer", sdp="v=0\r\n", peer_epoch=2)
    ) == ControlMessage(
        type="standby.offer",
        values={"peer_epoch": 2, "sdp": "v=0\r\n"},
    )
    for message_type in ("standby.promote", "standby.promoted", "standby.failed"):
        assert decode_packet(
            encode_control(message_type, peer_epoch=2)
        ) == ControlMessage(message_type, {"peer_epoch": 2})


@pytest.mark.parametrize("peer_epoch", [False, 0, -1, 0x1_0000_0000])
def test_ipc_rejects_invalid_standby_peer_epoch(peer_epoch: object) -> None:
    with pytest.raises(ProtocolError, match="peer epoch"):
        encode_control("standby.promote", peer_epoch=peer_epoch)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "message_type",
    ["standby.promote", "standby.offer", "standby.promoted", "standby.failed"],
)
def test_ipc_requires_epoch_on_every_tagged_standby_control(
    message_type: str,
) -> None:
    values = {"sdp": "v=0\r\n"} if message_type == "standby.offer" else {}
    with pytest.raises(ProtocolError, match="invalid fields"):
        encode_control(message_type, **values)


@pytest.mark.parametrize(
    "gain_db",
    [True, float("nan"), float("inf"), -0.01, 18.01, 10**400],
)
def test_ipc_rejects_invalid_capture_gain(gain_db: object) -> None:
    with pytest.raises(ProtocolError, match="capture gain"):
        encode_control("create_offer", direct_capture_gain_db=gain_db)  # type: ignore[arg-type]


def _raw_control_packet(message_type: str, **values: object) -> bytes:
    body = json.dumps(
        {"type": message_type, **values},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return bytes((CONTROL_KIND, WIRE_VERSION)) + body


def test_ipc_decode_rejects_huge_integer_capture_gain_as_protocol_error() -> None:
    with pytest.raises(ProtocolError, match="capture gain"):
        decode_packet(
            _raw_control_packet(
                "create_offer",
                direct_capture_gain_db=10**400,
            )
        )


def test_ipc_capture_metrics_have_one_bounded_content_free_shape() -> None:
    values = {
        "post_gain_max_peak": 32_768,
        "post_gain_max_rms": 12_345,
        "clipped_samples": 7,
        "clipped_frames": 2,
    }
    assert decode_packet(encode_control("capture.metrics", **values)) == ControlMessage(
        type="capture.metrics",
        values=values,
    )
    with pytest.raises(ProtocolError, match="invalid fields"):
        encode_control("capture.metrics", post_gain_max_peak=1)


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


@pytest.mark.parametrize(
    "event_type",
    [
        "capture.direction.inactive",
        "capture.direction.recvonly",
        "capture.direction.sendonly",
        "capture.direction.sendrecv",
        "capture.direction.unknown",
        "capture.outbound_active",
        "capture.rtp_started",
        "playback.rtp_started",
        "media.started",
        "media.quiet",
        "interrupt.fenced",
        "capture.metrics",
    ],
)
def test_provider_lifecycle_sanitizer_rejects_internal_namespace_spoofing(
    event_type: str,
) -> None:
    assert (
        sanitize_provider_lifecycle(json.dumps({"type": event_type, "generation": 4}))
        is None
    )


@pytest.mark.parametrize(
    "event_type",
    ["capture.rtp_started", "playback.rtp_started"],
)
def test_rtp_started_lifecycle_has_one_content_free_protocol_shape(
    event_type: str,
) -> None:
    assert decode_packet(
        encode_control("lifecycle", event_type=event_type, generation=0)
    ) == ControlMessage(
        type="lifecycle",
        values={"event_type": event_type, "generation": 0},
    )

    with pytest.raises(ProtocolError, match="RTP lifecycle"):
        encode_control("lifecycle", event_type=event_type)
    with pytest.raises(ProtocolError, match="RTP lifecycle"):
        encode_control(
            "lifecycle",
            event_type=event_type,
            generation=0,
            response_id="resp_1",
        )


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
            values={"direct_capture_gain_db": 0.0},
        )
        probe.send(encode_control("offer", sdp="v=0\r\n"))
        assert client.drain_messages() == [
            ControlMessage(type="offer", values={"sdp": "v=0\r\n"})
        ]
        client.request_standby_offer(direct_capture_gain_db=4.5)
        assert decode_packet(probe.recv(4096)) == ControlMessage(
            type="standby.create_offer",
            values={"direct_capture_gain_db": 4.5},
        )
        client.promote_standby(2)
        assert decode_packet(probe.recv(4096)) == ControlMessage(
            type="standby.promote",
            values={"peer_epoch": 2},
        )
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


def test_sidecar_zero_budget_close_transfers_child_to_bounded_reaper() -> None:
    class DeferredExitProcess(FakeProcess):
        def __init__(self) -> None:
            super().__init__()
            self.reaped = threading.Event()

        def poll(self) -> int | None:
            return self.return_code

        def wait(self, timeout: float | None = None) -> int:
            self.waits.append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired("sidecar", timeout)
            self.return_code = -9
            self.reaped.set()
            return self.return_code

    process = DeferredExitProcess()
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    client = WebRtcSidecarClient(parent, process)
    try:
        started_at = time.monotonic()
        client.close(timeout=0.0)

        assert time.monotonic() - started_at < 0.1
        assert process.terminated
        assert process.killed
        assert process.reaped.wait(1.0)
        assert process.waits[-1] is None
        assert client.closed
    finally:
        child.close()


def test_sidecar_closed_health_poll_detects_completed_standby_child() -> None:
    process = FakeProcess()
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    client = WebRtcSidecarClient(parent, process)
    try:
        assert client.closed is False
        process.return_code = 17

        assert client.closed is True
        client.close(timeout=0.0)
        assert process.waits == []
    finally:
        child.close()


class FakePeer:
    def __init__(
        self,
        *,
        emit_lifecycle: Any,
        emit_playback: Any,
        emit_capture_metrics: Any,
        emit_state: Any,
        emit_fatal: Any,
    ) -> None:
        self.emit_lifecycle = emit_lifecycle
        self.emit_playback = emit_playback
        self.emit_capture_metrics = emit_capture_metrics
        self.emit_state = emit_state
        self.emit_fatal = emit_fatal
        self.captures: list[CaptureAudio] = []
        self.answers: list[str] = []
        self.interruptions = 0
        self.stop_count = 0
        self.capture_gains: list[float] = []
        self.capture_commits = 0

    def set_capture_gain_db(self, value: float) -> None:
        self.capture_gains.append(value)

    async def create_offer(self) -> str:
        return "v=0\r\na=fake-offer\r\n"

    async def set_answer(self, sdp: str) -> None:
        self.answers.append(sdp)
        self.emit_state("connected")
        self.emit_state("data.ready")

    def feed_capture(self, value: CaptureAudio) -> None:
        self.captures.append(value)

    def commit_capture(self) -> None:
        self.capture_commits += 1
        self.emit_state("capture.ready")

    def interrupt_response(self) -> None:
        self.interruptions += 1

    async def stop(self) -> None:
        self.stop_count += 1


async def _recv_packet(transport: socket.socket) -> Any:
    value = await asyncio.get_running_loop().sock_recv(transport, 65_537)
    return decode_packet(value)


@pytest.mark.asyncio
async def test_runtime_drives_offer_answer_audio_interrupt_and_clean_shutdown() -> None:
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
            encode_control("create_offer", direct_capture_gain_db=6.25),
        )
        assert await _recv_packet(parent) == ControlMessage(
            type="offer",
            values={"sdp": "v=0\r\na=fake-offer\r\n"},
        )
        assert peers[0].capture_gains == [6.25]

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
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("capture.commit"),
        )
        assert await _recv_packet(parent) == ControlMessage(
            type="capture.ready",
            values={},
        )
        assert peers[0].capture_commits == 1

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
            encode_control("response.interrupt"),
        )
        async with asyncio.timeout(1):
            while not peers[0].captures or not peers[0].interruptions:
                await asyncio.sleep(0)
        assert peers[0].captures == [capture]
        assert peers[0].interruptions == 1

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
            encode_control("create_offer"),
        )
        assert await _recv_packet(parent) == ControlMessage(
            type="offer",
            values={"sdp": "v=0\r\na=fake-offer\r\n"},
        )
        assert len(peers) == 2
        assert peers[0].stop_count == 1
        assert peers[1].capture_gains == [0.0]
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("set_answer", sdp="v=0\r\na=fresh-answer\r\n"),
        )
        fresh_answer_events = [await _recv_packet(parent) for _ in range(3)]
        assert fresh_answer_events == [
            ControlMessage(type="connected", values={}),
            ControlMessage(type="data.ready", values={}),
            ControlMessage(type="answer.applied", values={}),
        ]
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("capture.commit"),
        )
        assert await _recv_packet(parent) == ControlMessage(
            type="capture.ready",
            values={},
        )
        assert peers[1].capture_commits == 1
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("shutdown"),
        )
        assert await _recv_packet(parent) == ControlMessage(
            type="shutdown.complete",
            values={},
        )
        assert await asyncio.wait_for(run_task, timeout=1) == 0
        assert peers[1].stop_count == 1
    finally:
        parent.close()
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_runtime_warms_and_promotes_second_peer_without_pausing_active_capture() -> (
    None
):
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent.setblocking(False)
    peers: list[FakePeer] = []
    standby_offer_started = asyncio.Event()
    release_standby_offer = asyncio.Event()

    class WarmPeer(FakePeer):
        def __init__(self, *, index: int, **callbacks: Any) -> None:
            super().__init__(**callbacks)
            self.index = index

        async def create_offer(self) -> str:
            if self.index == 1:
                standby_offer_started.set()
                await release_standby_offer.wait()
            return f"v=0\r\na=fake-offer-{self.index}\r\n"

    def peer_factory(**callbacks: Any) -> FakePeer:
        peer = WarmPeer(index=len(peers), **callbacks)
        peers.append(peer)
        return peer

    runtime = SidecarRuntime(child, peer_factory=peer_factory)
    run_task = asyncio.create_task(runtime.run())
    first_capture = CaptureAudio(0, 1, b"\x01\x00" * 320)
    second_capture = CaptureAudio(320, 2, b"\x02\x00" * 320)
    old_playback = PlaybackAudio(1, 0, 0, b"\x03\x00" * 480)
    new_playback = PlaybackAudio(1, 0, 0, b"\x04\x00" * 480)
    try:
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("create_offer"),
        )
        assert (await _recv_packet(parent)).type == "offer"

        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("standby.create_offer", direct_capture_gain_db=8.0),
        )
        await asyncio.wait_for(standby_offer_started.wait(), timeout=1)
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_capture_audio(
                first_capture.pcm,
                sample_index=first_capture.sample_index,
                capture_monotonic_ns=first_capture.capture_monotonic_ns,
            ),
        )
        async with asyncio.timeout(1):
            while peers[0].captures != [first_capture]:
                await asyncio.sleep(0)
        peers[0].emit_playback(old_playback)
        assert await _recv_packet(parent) == old_playback

        release_standby_offer.set()
        assert await _recv_packet(parent) == ControlMessage(
            "standby.offer",
            {
                "peer_epoch": 2,
                "sdp": "v=0\r\na=fake-offer-1\r\n",
            },
        )
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("standby.promote", peer_epoch=2),
        )
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_capture_audio(
                second_capture.pcm,
                sample_index=second_capture.sample_index,
                capture_monotonic_ns=second_capture.capture_monotonic_ns,
            ),
        )
        assert await _recv_packet(parent) == ControlMessage(
            "standby.promoted",
            {"peer_epoch": 2},
        )
        async with asyncio.timeout(1):
            while peers[1].captures != [second_capture]:
                await asyncio.sleep(0)
        assert peers[0].captures == [first_capture]
        assert peers[0].stop_count == 1

        # Retired callbacks carry their closed-over epoch and cannot cross the
        # promotion acknowledgement. Only the promoted peer remains observable.
        peers[0].emit_playback(old_playback)
        peers[0].emit_lifecycle({"event_type": "media.started", "generation": 9})
        peers[0].emit_fatal("connection_failed")
        peers[1].emit_playback(new_playback)
        assert await _recv_packet(parent) == new_playback
        assert not run_task.done()

        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("standby.create_offer"),
        )
        assert (await _recv_packet(parent)).type == "standby.offer"
        assert len(peers) == 3
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("shutdown"),
        )
        assert await _recv_packet(parent) == ControlMessage(
            "shutdown.complete",
            {},
        )
        assert await asyncio.wait_for(run_task, timeout=1) == 0
        assert peers[1].stop_count == 1
        assert peers[2].stop_count == 1
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
async def test_runtime_maps_huge_integer_capture_gain_to_protocol_error() -> None:
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent.setblocking(False)
    runtime = SidecarRuntime(child, peer_factory=FakePeer)
    run_task = asyncio.create_task(runtime.run())
    try:
        await asyncio.get_running_loop().sock_sendall(
            parent,
            _raw_control_packet(
                "create_offer",
                direct_capture_gain_db=10**400,
            ),
        )
        assert await _recv_packet(parent) == ControlMessage(
            type="error",
            values={"code": "protocol_error"},
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
        assert track.sender_sample_cursor == 1_000
        first, second = await asyncio.gather(track.recv(), track.recv())
        assert first.sample_rate == 48_000
        assert first.samples == 960
        assert first.pts == 0
        assert second.pts == 960
        assert first.time_base == second.time_base == Fraction(1, 48_000)
        assert track.consumed_samples == 640
        assert track.latest_sample_end == 1_640
        assert track.consumed_sample_end == 1_640
        assert track.sender_sample_cursor == 1_640
    finally:
        track.stop()


def test_capture_gain_scales_signed_pcm_saturates_and_preserves_unity() -> None:
    source = struct.pack("<hhhhh", 1_000, -1_000, 20_000, -20_000, 0)

    unity, unity_metrics = _apply_capture_gain_pcm16(source, 0.0)
    amplified, metrics = _apply_capture_gain_pcm16(source, 6.0)

    assert unity == source
    assert unity_metrics.clipped_samples == 0
    assert struct.unpack("<hhhhh", amplified) == (
        1_995,
        -1_995,
        32_767,
        -32_768,
        0,
    )
    assert metrics.max_peak == 32_768
    assert metrics.clipped_samples == 2
    assert metrics.clipped is True


def test_capture_gain_preserves_exact_silence() -> None:
    amplified, metrics = _apply_capture_gain_pcm16(bytes(640), 12.0)

    assert amplified == bytes(640)
    assert metrics.max_peak == 0
    assert metrics.rms == 0
    assert metrics.clipped_samples == 0
    assert metrics.clipped is False


def test_capture_metrics_are_rate_bounded_and_callback_failure_is_nonfatal() -> None:
    emitted: list[dict[str, int]] = []
    track = CaptureAudioTrack(on_metrics=emitted.append)
    clipped = peer_module.CaptureFrameMetrics(
        max_peak=32_768,
        rms=20_000,
        clipped_samples=10,
        clipped=True,
    )

    for _ in range(10):
        track._observe_metrics(clipped)

    assert emitted == [
        {
            "post_gain_max_peak": 32_768,
            "post_gain_max_rms": 20_000,
            "clipped_samples": 10,
            "clipped_frames": 1,
        }
    ]

    fatal: list[str] = []
    peer = object.__new__(DeviceWebRtcPeer)
    peer._failed = False
    peer._emit_capture_metrics = lambda _values: (_ for _ in ()).throw(BlockingIOError)
    peer._emit_fatal = fatal.append
    peer._safe_capture_metrics(emitted[0])

    assert fatal == []
    assert peer._failed is False


@pytest.mark.asyncio
async def test_capture_gain_applies_after_mixed_packet_frame_assembly() -> None:
    metrics: list[dict[str, int]] = []
    track = CaptureAudioTrack(capture_gain_db=6.0, on_metrics=metrics.append)
    captured_at = time.monotonic_ns()
    try:
        # The first half models retained rollover preroll and the second half
        # live capture. One sidecar frame assembly applies the same gain to both.
        track.feed(
            CaptureAudio(
                sample_index=0,
                capture_monotonic_ns=captured_at,
                pcm=(1_000).to_bytes(2, "little", signed=True) * 160,
            )
        )
        track.feed(
            CaptureAudio(
                sample_index=160,
                capture_monotonic_ns=captured_at + 10_000_000,
                pcm=(-1_000).to_bytes(2, "little", signed=True) * 160,
            )
        )

        frame = await track.recv()

        assert bytes(frame.planes[0])[:960] == (
            (1_995).to_bytes(2, "little", signed=True) * 480
        )
        assert bytes(frame.planes[0])[960:1_920] == (
            (-1_995).to_bytes(2, "little", signed=True) * 480
        )
        assert metrics == [
            {
                "post_gain_max_peak": 1_995,
                "post_gain_max_rms": 1_995,
                "clipped_samples": 0,
                "clipped_frames": 0,
            }
        ]
    finally:
        track.stop()


@pytest.mark.asyncio
async def test_capture_track_reframes_packets_into_single_payload_opus_frames() -> None:
    consumed: list[int] = []
    track = CaptureAudioTrack(on_consumed=consumed.append)
    captured_at = time.monotonic_ns()
    first_pcm = b"\x01\x00" * 1_024
    second_pcm = b"\x02\x00" * 1_024
    try:
        track.feed(
            CaptureAudio(
                sample_index=1_000,
                capture_monotonic_ns=captured_at,
                pcm=first_pcm,
            )
        )
        track.feed(
            CaptureAudio(
                sample_index=2_024,
                capture_monotonic_ns=captured_at + 64_000_000,
                pcm=second_pcm,
            )
        )

        frames = [await track.recv() for _ in range(6)]

        assert [frame.samples for frame in frames] == [960] * 6
        assert [frame.sample_rate for frame in frames] == [48_000] * 6
        assert [frame.pts for frame in frames] == list(range(0, 5_760, 960))
        assert [frame.time_base for frame in frames] == [Fraction(1, 48_000)] * 6

        source_pcm = first_pcm + second_pcm
        for index, frame in enumerate(frames):
            source_frame = source_pcm[index * 640 : (index + 1) * 640]
            expected_pcm = b"".join(
                source_frame[offset : offset + 2] * 3
                for offset in range(0, len(source_frame), 2)
            )
            assert bytes(frame.planes[0])[:1_920] == expected_pcm

        assert consumed == [320, 640, 960, 1_280, 1_600, 1_920]
        assert track.consumed_samples == 1_920
        assert track.latest_sample_end == 3_048
        assert track.consumed_sample_end == 2_920
        assert track.sender_sample_cursor == 2_920
        assert track._queued_samples == 128

        assert peer_module.aiortc is not None
        assert peer_module.aiortc.__version__ == "1.15.0"
        encoder = OpusEncoder()
        encoded = [encoder.encode(frame) for frame in frames]
        assert [len(payloads) for payloads, _timestamp in encoded] == [1] * 6
        assert [timestamp for _payloads, timestamp in encoded] == list(
            range(0, 5_760, 960)
        )
    finally:
        track.stop()


@pytest.mark.asyncio
async def test_capture_track_sends_silence_during_first_playback_aec_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [40_000_000_000]
    monkeypatch.setattr(peer_module.time, "monotonic_ns", lambda: now[0])
    track = CaptureAudioTrack(capture_gain_db=12.0)
    try:
        track.feed(
            CaptureAudio(
                sample_index=0,
                capture_monotonic_ns=now[0] - 20_000_000,
                pcm=b"\x04\x00" * 320,
            )
        )
        track.suppress_capture_for_playback_settle()

        pre_playback = await track.recv()

        assert bytes(pre_playback.planes[0])[:1_920] == b"\x0f\x00" * 960
        assert pre_playback.pts == 0

        now[0] += 1_000_000
        track.feed(
            CaptureAudio(
                sample_index=320,
                capture_monotonic_ns=now[0],
                pcm=b"\x05\x00" * 320,
            )
        )
        suppressed = await track.recv()

        assert bytes(suppressed.planes[0])[:1_920] == bytes(1_920)
        assert suppressed.pts == 960
        assert track.consumed_samples == 640

        now[0] += (peer_module.CAPTURE_ECHO_SETTLE_MILLISECONDS + 1) * 1_000_000
        track.feed(
            CaptureAudio(
                sample_index=640,
                capture_monotonic_ns=now[0],
                pcm=b"\x06\x00" * 320,
            )
        )

        audible = await track.recv()

        assert bytes(audible.planes[0])[:1_920] == b"\x17\x00" * 960
        assert audible.pts == 1_920
        assert track.consumed_samples == 960
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


def test_capture_track_duration_bound_covers_one_prewarmed_rollover() -> None:
    assert MAX_CAPTURE_QUEUE_MILLISECONDS == 2_048
    track = CaptureAudioTrack()
    captured_at = time.monotonic_ns()
    sample_index = 0
    try:
        # The pinned recorder emits at most 32 64-ms frames while a fresh peer
        # awaits its answer. Their exact 2.048 seconds remain RAM-bounded, while
        # the independent 2.25-second timestamp check still rejects stale PCM.
        for packet_index in range(32):
            track.feed(
                CaptureAudio(
                    sample_index=sample_index,
                    capture_monotonic_ns=captured_at + packet_index,
                    pcm=b"\x00\x00" * 1_024,
                )
            )
            sample_index += 1_024
        with pytest.raises(PeerBackpressure, match="duration bound"):
            track.feed(
                CaptureAudio(
                    sample_index=32_768,
                    capture_monotonic_ns=captured_at + 32,
                    pcm=b"\x00\x00",
                )
            )
    finally:
        track.stop()


def test_capture_track_rejects_audio_older_than_startup_bound() -> None:
    track = CaptureAudioTrack()
    try:
        stale_at = (
            time.monotonic_ns() - (MAX_STARTUP_CAPTURE_AGE_MILLISECONDS + 1) * 1_000_000
        )
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


@pytest.mark.asyncio
async def test_capture_track_accepts_negotiation_backlog_beyond_runtime_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_at = 10_000_000_000
    now = [captured_at]
    monkeypatch.setattr(peer_module.time, "monotonic_ns", lambda: now[0])
    track = CaptureAudioTrack()
    track.feed(
        CaptureAudio(
            sample_index=0,
            capture_monotonic_ns=captured_at,
            pcm=b"\x01\x00" * 320,
        )
    )

    try:
        now[0] += (MAX_CAPTURE_AGE_MILLISECONDS + 1) * 1_000_000
        frame = await track.recv()

        assert frame.samples == 960
        assert track.consumed_samples == 320
    finally:
        track.stop()


@pytest.mark.asyncio
async def test_capture_track_first_recv_does_not_overtake_later_precommit_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [10_000_000_000]
    monkeypatch.setattr(peer_module.time, "monotonic_ns", lambda: now[0])
    track = CaptureAudioTrack()
    try:
        track.feed(
            CaptureAudio(
                sample_index=0,
                capture_monotonic_ns=7_500_000_000,
                pcm=b"\x01\x00" * 320,
            )
        )
        await track.recv()

        now[0] = 11_000_000_000
        track.feed(
            CaptureAudio(
                sample_index=320,
                capture_monotonic_ns=8_000_000_000,
                pcm=b"\x02\x00" * 320,
            )
        )
        assert track.commit_startup_capture() == 640
        frame = await track.recv()

        assert frame.samples == 960
        assert track.consumed_sample_end == 640
    finally:
        track.stop()


@pytest.mark.asyncio
async def test_capture_track_commit_cutoff_selects_exact_runtime_age_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [20_000_000_000]
    monkeypatch.setattr(peer_module.time, "monotonic_ns", lambda: now[0])
    track = CaptureAudioTrack()
    try:
        track.feed(
            CaptureAudio(
                sample_index=0,
                capture_monotonic_ns=17_000_000_000,
                pcm=b"\x01\x00" * 320,
            )
        )
        assert track.commit_startup_capture() == 320
        # A packet ending exactly at the exclusive cursor is pre-commit; the
        # next packet beginning there is post-commit and gets the runtime bound.
        assert track._capture_age_limit_ns(319) == (
            MAX_STARTUP_CAPTURE_AGE_MILLISECONDS * 1_000_000
        )
        assert track._capture_age_limit_ns(320) == (
            MAX_CAPTURE_AGE_MILLISECONDS * 1_000_000
        )
        track.feed(
            CaptureAudio(
                sample_index=320,
                capture_monotonic_ns=(
                    now[0] - MAX_CAPTURE_AGE_MILLISECONDS * 1_000_000
                ),
                pcm=b"\x02\x00" * 320,
            )
        )
        post_cutoff_at = now[0] - 1_000_000_000
        track.feed(
            CaptureAudio(
                sample_index=640,
                capture_monotonic_ns=post_cutoff_at,
                pcm=b"\x03\x00" * 320,
            )
        )

        await track.recv()
        await track.recv()
        await track.recv()
        now[0] = post_cutoff_at + (MAX_CAPTURE_AGE_MILLISECONDS + 1) * 1_000_000
        with pytest.raises(PeerBackpressure, match="age bound"):
            track.feed(
                CaptureAudio(
                    sample_index=960,
                    capture_monotonic_ns=post_cutoff_at,
                    pcm=b"\x04\x00" * 320,
                )
            )
    finally:
        track.stop()


@pytest.mark.asyncio
async def test_capture_track_revalidates_runtime_age_after_startup_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_at = 15_000_000_000
    now = [captured_at]
    monkeypatch.setattr(peer_module.time, "monotonic_ns", lambda: now[0])
    track = CaptureAudioTrack()
    track.feed(
        CaptureAudio(
            sample_index=0,
            capture_monotonic_ns=captured_at,
            pcm=b"\x01\x00" * 320,
        )
    )
    await track.recv()
    track.commit_startup_capture()
    track.feed(
        CaptureAudio(
            sample_index=320,
            capture_monotonic_ns=captured_at,
            pcm=b"\x01\x00" * 320,
        )
    )

    now[0] += (MAX_CAPTURE_AGE_MILLISECONDS + 1) * 1_000_000
    with pytest.raises(PeerBackpressure, match="before RTP consumption"):
        await track.recv()

    assert track.consumed_samples == 320
    assert track._queued_samples == 0
    with pytest.raises(PeerError, match="stopped"):
        track.feed(
            CaptureAudio(
                sample_index=640,
                capture_monotonic_ns=now[0],
                pcm=b"\x01\x00" * 320,
            )
        )


@pytest.mark.asyncio
async def test_capture_track_accepts_exact_age_bound_at_rtp_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_at = 20_000_000_000
    now = [captured_at]
    monkeypatch.setattr(peer_module.time, "monotonic_ns", lambda: now[0])
    track = CaptureAudioTrack()
    try:
        track.feed(
            CaptureAudio(
                sample_index=0,
                capture_monotonic_ns=captured_at,
                pcm=b"\x01\x00" * 320,
            )
        )
        await track.recv()
        track.commit_startup_capture()
        track.feed(
            CaptureAudio(
                sample_index=320,
                capture_monotonic_ns=captured_at,
                pcm=b"\x01\x00" * 320,
            )
        )
        now[0] += MAX_CAPTURE_AGE_MILLISECONDS * 1_000_000

        frame = await track.recv()

        assert frame.samples == 960
        assert frame.sample_rate == 48_000
        assert track.consumed_samples == 640
    finally:
        track.stop()


@pytest.mark.asyncio
async def test_delayed_capture_consumption_emits_terminal_peer_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_at = 30_000_000_000
    now = [captured_at]
    monkeypatch.setattr(peer_module.time, "monotonic_ns", lambda: now[0])
    peer, emitted = _fake_device_peer(monkeypatch)
    peer.input_track.feed(
        CaptureAudio(
            sample_index=0,
            capture_monotonic_ns=captured_at,
            pcm=b"\x01\x00" * 320,
        )
    )
    await peer.input_track.recv()
    peer.commit_capture()
    emitted.clear()
    peer.input_track.feed(
        CaptureAudio(
            sample_index=320,
            capture_monotonic_ns=captured_at,
            pcm=b"\x01\x00" * 320,
        )
    )

    now[0] += (MAX_CAPTURE_AGE_MILLISECONDS + 1) * 1_000_000
    with pytest.raises(PeerBackpressure, match="before RTP consumption"):
        await peer.input_track.recv()

    assert emitted == [("fatal", "capture_audio_stale")]
    assert peer._failed
    assert peer._muted


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
        self.receivers: list[Any] = []
        self.negotiated_direction = "sendrecv"
        self.capture_transceiver = SimpleNamespace(currentDirection=None)
        self.stats_reports: list[dict[str, Any]] = []
        self.stats_error: Exception | None = None

    def on(self, event: str) -> Any:
        def decorate(handler: Any) -> Any:
            self.handlers[event] = handler
            return handler

        return decorate

    def addTransceiver(self, track: Any, *, direction: str) -> object:
        self.transceiver = (track, direction)
        return self.capture_transceiver

    def createDataChannel(self, label: str, *, ordered: bool) -> FakeChannel:
        assert label == "oai-events"
        assert ordered is True
        self.channel = FakeChannel()
        return self.channel

    def getReceivers(self) -> list[Any]:
        return self.receivers

    async def createOffer(self) -> object:
        return object()

    async def setLocalDescription(self, offer: object) -> None:
        del offer

    async def setRemoteDescription(self, answer: object) -> None:
        self.answer = answer
        self.capture_transceiver.currentDirection = self.negotiated_direction

    async def getStats(self) -> dict[str, Any]:
        if self.stats_error is not None:
            raise self.stats_error
        if self.stats_reports:
            return self.stats_reports.pop(0)
        return {}

    async def close(self) -> None:
        self.connectionState = "closed"


def _fake_device_peer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    emit_lifecycle: Any | None = None,
) -> tuple[DeviceWebRtcPeer, list[Any]]:
    emitted: list[Any] = []

    class Configuration:
        def __init__(self, *, iceServers: list[Any]) -> None:
            self.iceServers = iceServers

    monkeypatch.setattr(peer_module, "RTCConfiguration", Configuration)
    monkeypatch.setattr(peer_module, "RTCPeerConnection", FakePeerConnection)
    # Most protocol tests use tiny integers as readable stand-ins for audible
    # PCM. Dedicated signal-gate regressions below restore production bounds.
    monkeypatch.setattr(peer_module, "PLAYBACK_SIGNAL_PEAK", 1)
    monkeypatch.setattr(peer_module, "PLAYBACK_SIGNAL_RMS", 1)
    if emit_lifecycle is None:

        def lifecycle_emitter(value: dict[str, str | int]) -> None:
            emitted.append(("lifecycle", value))

    else:
        lifecycle_emitter = emit_lifecycle
    peer = DeviceWebRtcPeer(
        emit_lifecycle=lifecycle_emitter,
        emit_playback=lambda value: emitted.append(("playback", value)),
        emit_capture_metrics=lambda _value: None,
        emit_state=lambda value: emitted.append(("state", value)),
        emit_fatal=lambda value: emitted.append(("fatal", value)),
    )
    decoder_tracker = peer_module._TrackedDecoderQueue(thread_queue.Queue())
    receiver = SimpleNamespace(track=None)
    receiver._RTCRtpReceiver__decoder_queue = decoder_tracker
    receiver._RTCRtpReceiver__jitter_buffer = peer_module.JitterBuffer(
        capacity=16,
        prefetch=4,
    )
    peer._decoder_queue = decoder_tracker
    peer._audio_receiver = receiver
    return peer, emitted


@pytest.mark.asyncio
async def test_peer_reports_first_capture_rtp_consumption_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer, emitted = _fake_device_peer(monkeypatch)
    captured_at = time.monotonic_ns()
    try:
        peer.feed_capture(
            CaptureAudio(
                sample_index=0,
                capture_monotonic_ns=captured_at,
                pcm=b"\x01\x00" * 640,
            )
        )
        peer.commit_capture()

        assert emitted == []
        await peer.input_track.recv()
        assert emitted == [
            (
                "lifecycle",
                {"event_type": "capture.rtp_started", "generation": 0},
            )
        ]
        await peer.input_track.recv()

        assert emitted == [
            (
                "lifecycle",
                {"event_type": "capture.rtp_started", "generation": 0},
            ),
            ("state", "capture.ready"),
        ]
    finally:
        await peer.stop()


@pytest.mark.asyncio
async def test_peer_zero_packet_and_already_consumed_commits_are_immediately_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_peer, empty_emitted = _fake_device_peer(monkeypatch)
    consumed_peer, consumed_emitted = _fake_device_peer(monkeypatch)
    try:
        empty_peer.commit_capture()
        assert empty_emitted == [("state", "capture.ready")]

        consumed_peer.feed_capture(
            CaptureAudio(
                sample_index=0,
                capture_monotonic_ns=time.monotonic_ns(),
                pcm=b"\x01\x00" * 320,
            )
        )
        await consumed_peer.input_track.recv()
        consumed_peer.commit_capture()
        assert consumed_emitted == [
            (
                "lifecycle",
                {"event_type": "capture.rtp_started", "generation": 0},
            ),
            ("state", "capture.ready"),
        ]
    finally:
        await empty_peer.stop()
        await consumed_peer.stop()


@pytest.mark.asyncio
async def test_peer_fatal_capture_lifecycle_suppresses_capture_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_lifecycle(_values: dict[str, str | int]) -> None:
        raise BlockingIOError

    peer, emitted = _fake_device_peer(
        monkeypatch,
        emit_lifecycle=fail_lifecycle,
    )
    try:
        peer.feed_capture(
            CaptureAudio(
                sample_index=0,
                capture_monotonic_ns=time.monotonic_ns(),
                pcm=b"\x01\x00" * 320,
            )
        )
        peer.commit_capture()
        await peer.input_track.recv()

        assert emitted == [("fatal", "lifecycle_output_failed")]
        assert peer._failed
        assert not peer._capture_ready_emitted
    finally:
        await peer.stop()


@pytest.mark.asyncio
async def test_runtime_latched_fatal_beats_synchronous_capture_ready() -> None:
    class FatalCommitPeer(FakePeer):
        def commit_capture(self) -> None:
            self.emit_fatal("capture_audio_stale")
            self.emit_state("capture.ready")
            raise PeerError("commit failed after fatal")

    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent.setblocking(False)
    runtime = SidecarRuntime(child, peer_factory=FatalCommitPeer)
    run_task = asyncio.create_task(runtime.run())
    try:
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("create_offer"),
        )
        assert isinstance(await _recv_packet(parent), ControlMessage)
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("set_answer", sdp="v=0\r\n"),
        )
        for _ in range(3):
            assert isinstance(await _recv_packet(parent), ControlMessage)
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("capture.commit"),
        )

        assert await _recv_packet(parent) == ControlMessage(
            type="error",
            values={"code": "capture_audio_stale"},
        )
        assert await asyncio.wait_for(run_task, timeout=1) == 1
    finally:
        parent.close()
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_runtime_reports_delayed_capture_consumption_as_terminal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_at = 40_000_000_000
    now = [captured_at]
    monkeypatch.setattr(peer_module.time, "monotonic_ns", lambda: now[0])
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent.setblocking(False)
    peers: list[DeviceWebRtcPeer] = []

    def peer_factory(**callbacks: Any) -> DeviceWebRtcPeer:
        peer, _emitted = _fake_device_peer(monkeypatch)
        peer._emit_lifecycle = callbacks["emit_lifecycle"]
        peer._emit_playback = callbacks["emit_playback"]
        peer._emit_state = callbacks["emit_state"]
        peer._emit_fatal = callbacks["emit_fatal"]
        peers.append(peer)
        return peer

    runtime = SidecarRuntime(child, peer_factory=peer_factory)
    run_task = asyncio.create_task(runtime.run())
    try:
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_control("create_offer"),
        )
        offer = await _recv_packet(parent)
        assert isinstance(offer, ControlMessage)
        assert offer.type == "offer"

        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_capture_audio(
                b"\x01\x00" * 320,
                sample_index=0,
                capture_monotonic_ns=captured_at,
            ),
        )
        await _wait_for(lambda: peers[0].input_track.latest_sample_end == 320)
        await peers[0].input_track.recv()
        peers[0].input_track.commit_startup_capture()
        assert await _recv_packet(parent) == ControlMessage(
            type="lifecycle",
            values={"event_type": "capture.rtp_started", "generation": 0},
        )
        await asyncio.get_running_loop().sock_sendall(
            parent,
            encode_capture_audio(
                b"\x01\x00" * 320,
                sample_index=320,
                capture_monotonic_ns=captured_at,
            ),
        )
        await _wait_for(lambda: peers[0].input_track.latest_sample_end == 640)
        now[0] += (MAX_CAPTURE_AGE_MILLISECONDS + 1) * 1_000_000

        with pytest.raises(PeerBackpressure, match="before RTP consumption"):
            await peers[0].input_track.recv()

        assert await _recv_packet(parent) == ControlMessage(
            type="error",
            values={"code": "capture_audio_stale"},
        )
        assert await asyncio.wait_for(run_task, timeout=1) == 1
    finally:
        parent.close()
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)


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


@pytest.mark.asyncio
async def test_peer_reports_negotiated_send_direction_and_outbound_counter_movement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "OUTBOUND_CAPTURE_PROOF_POLL_SECONDS", 0.001)
    peer, emitted = _fake_device_peer(monkeypatch)
    peer.pc.negotiated_direction = "sendonly"
    peer.pc.stats_reports = [
        {
            "outbound-audio": SimpleNamespace(
                type="outbound-rtp",
                kind="audio",
                packetsSent=0,
                bytesSent=0,
                ssrc=4_294_967_295,
                transportId="private-transport-id",
                trackId="private-track-id",
            )
        },
        {
            "outbound-audio": SimpleNamespace(
                type="outbound-rtp",
                kind="audio",
                packetsSent=1,
                bytesSent=72,
                ssrc=4_294_967_295,
                transportId="private-transport-id",
                trackId="private-track-id",
            )
        },
    ]
    try:
        await peer.set_answer("v=0\r\na=private-answer\r\n")
        proof_task = peer._outbound_capture_proof_task
        assert proof_task is not None
        await asyncio.wait_for(proof_task, timeout=1)

        assert emitted == [
            (
                "lifecycle",
                {"event_type": "capture.direction.sendonly", "generation": 0},
            ),
            (
                "lifecycle",
                {"event_type": "capture.outbound_active", "generation": 0},
            ),
        ]
        serialized = json.dumps(emitted)
        assert "private-answer" not in serialized
        assert "private-transport-id" not in serialized
        assert "private-track-id" not in serialized
        assert "4294967295" not in serialized
        assert "packetsSent" not in serialized
        assert "bytesSent" not in serialized
    finally:
        await peer.stop()


@pytest.mark.asyncio
async def test_peer_outbound_proof_requires_both_counters_and_is_nonfatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "OUTBOUND_CAPTURE_PROOF_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(peer_module, "OUTBOUND_CAPTURE_PROOF_POLL_SECONDS", 0.001)
    peer, emitted = _fake_device_peer(monkeypatch)
    peer.pc.stats_reports = [
        {
            "outbound-audio": SimpleNamespace(
                type="outbound-rtp",
                kind="audio",
                packetsSent=4,
                bytesSent=0,
            )
        }
    ]
    try:
        await peer.set_answer("v=0\r\n")
        proof_task = peer._outbound_capture_proof_task
        assert proof_task is not None
        await asyncio.wait_for(proof_task, timeout=1)

        assert emitted == [
            (
                "lifecycle",
                {"event_type": "capture.direction.sendrecv", "generation": 0},
            )
        ]
        assert peer._failed is False
    finally:
        await peer.stop()


@pytest.mark.asyncio
async def test_peer_unavailable_outbound_stats_do_not_fail_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer, emitted = _fake_device_peer(monkeypatch)
    peer.pc.stats_error = RuntimeError("stats unavailable")
    try:
        await peer.set_answer("v=0\r\n")
        proof_task = peer._outbound_capture_proof_task
        assert proof_task is not None
        await asyncio.wait_for(proof_task, timeout=1)

        assert emitted == [
            (
                "lifecycle",
                {"event_type": "capture.direction.sendrecv", "generation": 0},
            )
        ]
        assert peer._failed is False
    finally:
        await peer.stop()


class PassthroughResampler:
    def __init__(self, **kwargs: Any) -> None:
        assert kwargs == {"format": "s16", "layout": "mono", "rate": 24_000}

    def resample(self, frame: object) -> list[object]:
        return [frame]


class BufferingResampler:
    def __init__(self, **kwargs: Any) -> None:
        assert kwargs == {"format": "s16", "layout": "mono", "rate": 24_000}

    def resample(self, frame: object) -> list[object]:
        del frame
        return []


class OneFrameDelayResampler:
    def __init__(self, **kwargs: Any) -> None:
        assert kwargs == {"format": "s16", "layout": "mono", "rate": 24_000}
        self._previous: object | None = None

    def resample(self, frame: object) -> list[object]:
        previous = self._previous
        self._previous = frame
        return [] if previous is None else [previous]


class QueuedRemoteTrack:
    kind = "audio"

    def __init__(self) -> None:
        self._queue = peer_module._TrackedRemoteQueue(asyncio.Queue())
        self.received = 0

    @property
    def frames(self) -> Any:
        return self._queue

    async def recv(self) -> object:
        value = await self._queue.get()
        if value is None:
            raise peer_module.MediaStreamError
        self.received += 1
        return value


class RawRemoteTrack:
    kind = "audio"

    def __init__(self) -> None:
        self._queue: Any = asyncio.Queue()

    async def recv(self) -> object:
        return await self._queue.get()


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


@pytest.mark.asyncio
async def test_peer_track_handler_installs_pinned_decoder_queue_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = RawRemoteTrack()
    receiver = SimpleNamespace(track=track)
    receiver._RTCRtpReceiver__decoder_queue = thread_queue.Queue()
    receiver._RTCRtpReceiver__jitter_buffer = peer_module.JitterBuffer(
        capacity=16,
        prefetch=4,
    )
    peer.pc.receivers = [receiver]
    peer._decoder_queue = None
    peer._audio_receiver = None

    peer.pc.handlers["track"](track)
    await asyncio.sleep(0)

    assert isinstance(track._queue, peer_module._TrackedRemoteQueue)
    assert isinstance(
        receiver._RTCRtpReceiver__decoder_queue,
        peer_module._TrackedDecoderQueue,
    )
    assert peer._receiver_queue is track._queue
    assert peer._remote_audio_track_seen
    assert not any(kind == "fatal" for kind, _value in emitted)
    await peer.stop()


def test_peer_track_handler_rejects_nonempty_untracked_decoder_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer, emitted = _fake_device_peer(monkeypatch)
    track = RawRemoteTrack()
    track._queue.put_nowait(_remote_frame(27, pts=0))

    peer.pc.handlers["track"](track)

    assert emitted == [("fatal", "unsupported_receiver_boundary")]
    assert not peer._remote_audio_track_seen
    peer.input_track.stop()


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
                    "code": "buffer_not_active",
                    "param": "output_audio_buffer",
                    "message": "private provider details",
                },
            }
        )
    ) == {
        "event_type": "error",
        "error_event_id": "codex_device_cancel_1",
        "error_type": "invalid_request_error",
        "error_code": "buffer_not_active",
        "error_param": "output_audio_buffer",
    }
    assert sanitize_provider_lifecycle(
        '{"type":"response.done","response":{"status":"private text"}}'
    ) == {"event_type": "response.done"}


@pytest.mark.asyncio
async def test_peer_reports_first_received_playback_rtp_once_before_signal_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        track.frames.put_nowait(_remote_frame(0, pts=0))
        track.frames.put_nowait(_remote_frame(0, pts=480))
        await _wait_for(lambda: track.received == 2)

        assert emitted == [
            (
                "lifecycle",
                {"event_type": "playback.rtp_started", "generation": 0},
            )
        ]
        assert peer._generation == 0
        assert not peer._media_generation_open
    finally:
        await _close_audio_peer(peer, task)


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
        assert emitted[:3] == [
            (
                "lifecycle",
                {"event_type": "playback.rtp_started", "generation": 0},
            ),
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
async def test_peer_exact_zero_pcm_waits_for_first_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        for pts in range(0, 1_920, 480):
            track.frames.put_nowait(_remote_frame(0, pts=pts))
        await _wait_for(lambda: track.received == 4)

        assert emitted == [
            (
                "lifecycle",
                {"event_type": "playback.rtp_started", "generation": 0},
            )
        ]
        assert peer._generation == 0
        assert not peer._media_generation_open

        track.frames.put_nowait(_remote_frame(11, pts=1_920))
        await _wait_for(lambda: any(kind == "playback" for kind, _ in emitted))

        assert emitted == [
            (
                "lifecycle",
                {"event_type": "playback.rtp_started", "generation": 0},
            ),
            ("lifecycle", {"event_type": "media.started", "generation": 1}),
            (
                "playback",
                PlaybackAudio(
                    generation=1,
                    sample_index=0,
                    media_timestamp=1_920,
                    pcm=b"\x0b\x00" * 480,
                ),
            ),
        ]
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.parametrize(
    ("pcm", "expected"),
    [
        (b"", False),
        (b"\x00\x00" * 480, False),
        (b"\x01\x00" * 480, False),
        (b"\x3f\x00" * 480, False),
        (b"\x40\x00" + b"\x00\x00" * 63, True),
        (b"\x40\x00" * 480, True),
        (b"\x40", False),
    ],
)
def test_playback_signal_gate_rejects_subaudible_decode_residue(
    pcm: bytes,
    expected: bool,
) -> None:
    assert peer_module._pcm_has_playback_signal(pcm) is expected


@pytest.mark.asyncio
async def test_peer_subaudible_pcm_waits_for_first_audible_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    peer, emitted = _fake_device_peer(monkeypatch)
    settle_observations: list[list[tuple[str, Any]]] = []
    monkeypatch.setattr(
        peer.input_track,
        "suppress_capture_for_playback_settle",
        lambda: settle_observations.append(list(emitted)),
    )
    monkeypatch.setattr(peer_module, "PLAYBACK_SIGNAL_PEAK", 64)
    monkeypatch.setattr(peer_module, "PLAYBACK_SIGNAL_RMS", 8)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        for pts in range(0, 1_920, 480):
            track.frames.put_nowait(_remote_frame(4, pts=pts))
        await _wait_for(lambda: track.received == 4)

        rtp_started = (
            "lifecycle",
            {"event_type": "playback.rtp_started", "generation": 0},
        )
        assert emitted == [rtp_started]
        assert settle_observations == []
        assert peer._generation == 0
        assert not peer._media_generation_open

        track.frames.put_nowait(_remote_frame(128, pts=1_920))
        await _wait_for(lambda: any(kind == "playback" for kind, _ in emitted))

        assert emitted == [
            rtp_started,
            ("lifecycle", {"event_type": "media.started", "generation": 1}),
            (
                "playback",
                PlaybackAudio(
                    generation=1,
                    sample_index=0,
                    media_timestamp=1_920,
                    pcm=b"\x80\x00" * 480,
                ),
            ),
        ]
        assert settle_observations == [[rtp_started]]
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
        await _wait_for(
            lambda: len([kind for kind, _value in emitted if kind == "playback"]) == 1
        )
        _provider_event(peer, type="output_audio_buffer.stopped", response_id="resp_1")
        track.frames.put_nowait(_remote_frame(5, pts=480))
        await _wait_for(
            lambda: len([kind for kind, _value in emitted if kind == "playback"]) == 2
        )

        playback = [value for kind, value in emitted if kind == "playback"]
        assert [(value.generation, value.sample_index) for value in playback] == [
            (1, 0),
            (1, 480),
        ]
        assert playback[-1].pcm == b"\x05\x00" * 480
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
        await _wait_for(
            lambda: len([kind for kind, _value in emitted if kind == "playback"]) == 1
        )
        _provider_event(
            peer,
            type="response.created",
            response={"id": "resp_2", "status": "in_progress"},
        )
        _provider_event(peer, type="output_audio_buffer.started", response_id="resp_2")
        track.frames.put_nowait(_remote_frame(7, pts=480))
        await _wait_for(
            lambda: len([kind for kind, _value in emitted if kind == "playback"]) == 2
        )

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
    settle_calls: list[None] = []
    monkeypatch.setattr(
        peer.input_track,
        "suppress_capture_for_playback_settle",
        lambda: settle_calls.append(None),
    )
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
        await _wait_for(
            lambda: any(
                kind == "lifecycle"
                and value["event_type"] == "media.started"
                and value["generation"] == 2
                for kind, value in emitted
            )
        )

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
        assert settle_calls == [None]
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_exact_zero_pcm_does_not_extend_open_media_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    monkeypatch.setattr(peer_module, "MEDIA_QUIET_SECONDS", 0.05)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    zero_feeder: asyncio.Task[None] | None = None
    try:
        track.frames.put_nowait(_remote_frame(12, pts=0))
        await _wait_for(lambda: any(kind == "playback" for kind, _ in emitted))

        async def feed_continuous_zero_pcm() -> None:
            for pts in range(480, 96_480, 480):
                track.frames.put_nowait(_remote_frame(0, pts=pts))
                await asyncio.sleep(0.005)

        zero_feeder = asyncio.create_task(feed_continuous_zero_pcm())
        await _wait_for(lambda: track.received >= 2)
        await _wait_for(
            lambda: any(
                kind == "lifecycle" and value["event_type"] == "media.quiet"
                for kind, value in emitted
            )
        )

        assert not zero_feeder.done()
        assert not peer._media_generation_open
        assert [
            (value["event_type"], value["generation"])
            for kind, value in emitted
            if kind == "lifecycle"
        ] == [
            ("playback.rtp_started", 0),
            ("media.started", 1),
            ("media.quiet", 1),
        ]
        assert [value for kind, value in emitted if kind == "playback"] == [
            PlaybackAudio(
                generation=1,
                sample_index=0,
                media_timestamp=0,
                pcm=b"\x0c\x00" * 480,
            )
        ]
    finally:
        if zero_feeder is not None:
            zero_feeder.cancel()
            await asyncio.gather(zero_feeder, return_exceptions=True)
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_continuous_subaudible_pcm_does_not_extend_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    monkeypatch.setattr(peer_module, "MEDIA_QUIET_SECONDS", 0.05)
    peer, emitted = _fake_device_peer(monkeypatch)
    monkeypatch.setattr(peer_module, "PLAYBACK_SIGNAL_PEAK", 64)
    monkeypatch.setattr(peer_module, "PLAYBACK_SIGNAL_RMS", 8)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    residue_feeder: asyncio.Task[None] | None = None
    try:
        track.frames.put_nowait(_remote_frame(128, pts=0))
        await _wait_for(lambda: any(kind == "playback" for kind, _ in emitted))

        async def feed_continuous_residue() -> None:
            for pts in range(480, 96_480, 480):
                track.frames.put_nowait(_remote_frame(4, pts=pts))
                await asyncio.sleep(0.005)

        residue_feeder = asyncio.create_task(feed_continuous_residue())
        await _wait_for(lambda: track.received >= 2)
        await _wait_for(
            lambda: any(
                kind == "lifecycle" and value["event_type"] == "media.quiet"
                for kind, value in emitted
            )
        )

        assert not residue_feeder.done()
        assert not peer._media_generation_open
        assert [
            (value["event_type"], value["generation"])
            for kind, value in emitted
            if kind == "lifecycle"
        ] == [
            ("playback.rtp_started", 0),
            ("media.started", 1),
            ("media.quiet", 1),
        ]
        assert [value for kind, value in emitted if kind == "playback"] == [
            PlaybackAudio(
                generation=1,
                sample_index=0,
                media_timestamp=0,
                pcm=b"\x80\x00" * 480,
            )
        ]
    finally:
        if residue_feeder is not None:
            residue_feeder.cancel()
            await asyncio.gather(residue_feeder, return_exceptions=True)
        await _close_audio_peer(peer, task)


@pytest.mark.parametrize(
    "event_type",
    ["error", "invalid_request_error", "server_error"],
)
def test_peer_provider_errors_are_sanitized_and_fatal_without_local_controls(
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
) -> None:
    peer, emitted = _fake_device_peer(monkeypatch)
    try:
        _provider_event(
            peer,
            type=event_type,
            error={
                "event_id": "codex_device_cancel_legacy",
                "type": "invalid_request_error",
                "code": "invalid_value",
                "param": "type",
                "message": "private provider details",
            },
        )
        assert emitted == [
            (
                "lifecycle",
                {
                    "event_type": "error",
                    "generation": 0,
                    "error_event_id": "codex_device_cancel_legacy",
                    "error_type": "invalid_request_error",
                    "error_code": "invalid_value",
                    "error_param": "type",
                },
            ),
            ("fatal", "provider_error"),
        ]
        assert "private" not in json.dumps(emitted)
        assert peer.data_channel.sent == []
    finally:
        peer.input_track.stop()


def test_peer_drops_provider_spoofs_of_internal_lifecycle_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer, emitted = _fake_device_peer(monkeypatch)
    try:
        for event_type in (
            "capture.rtp_started",
            "playback.rtp_started",
            "media.started",
            "media.quiet",
            "interrupt.fenced",
        ):
            _provider_event(peer, type=event_type, generation=0xFFFFFFFF)
        assert emitted == []
    finally:
        peer.input_track.stop()


async def _consume_capture(
    peer: DeviceWebRtcPeer,
    *,
    sample_index: int,
    samples: int,
) -> None:
    _feed_capture(peer, sample_index=sample_index, samples=samples)
    await _consume_queued_capture(peer, samples=samples)


def _feed_capture(
    peer: DeviceWebRtcPeer,
    *,
    sample_index: int,
    samples: int,
) -> None:
    peer.feed_capture(
        CaptureAudio(
            sample_index=sample_index,
            capture_monotonic_ns=time.monotonic_ns(),
            pcm=b"\x01\x00" * samples,
        )
    )


async def _consume_queued_capture(
    peer: DeviceWebRtcPeer,
    *,
    samples: int,
) -> None:
    assert samples % peer_module.CAPTURE_FRAME_SAMPLES == 0
    for _ in range(samples // peer_module.CAPTURE_FRAME_SAMPLES):
        frame = await peer.input_track.recv()
        assert frame.samples == peer_module.CAPTURE_RTP_FRAME_SAMPLES
        assert frame.sample_rate == peer_module.CAPTURE_RTP_SAMPLE_RATE


async def _prime_capture_sender(
    peer: DeviceWebRtcPeer,
    *,
    samples: int = 320,
) -> int:
    """Establish a sender cursor, returning its next contiguous sample index."""
    await _consume_capture(peer, sample_index=0, samples=samples)
    return samples


def _use_fast_fence(
    monkeypatch: pytest.MonkeyPatch,
    *,
    guard: float = 0.03,
    quiet: float = 0.02,
    timeout: float = 0.20,
) -> None:
    monkeypatch.setattr(peer_module, "MEDIA_FENCE_MINIMUM_GUARD_SECONDS", guard)
    monkeypatch.setattr(peer_module, "MEDIA_FENCE_QUIET_SECONDS", quiet)
    monkeypatch.setattr(peer_module, "MEDIA_FENCE_TIMEOUT_SECONDS", timeout)


def _has_fenced(emitted: list[Any]) -> bool:
    return any(
        kind == "lifecycle" and value["event_type"] == "interrupt.fenced"
        for kind, value in emitted
    )


@pytest.mark.asyncio
async def test_peer_overdue_deadline_wins_after_event_loop_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fast_fence(monkeypatch, guard=0.10, quiet=0.10, timeout=0.03)
    peer, emitted = _fake_device_peer(monkeypatch)
    try:
        next_sample_index = await _prime_capture_sender(peer)
        _feed_capture(peer, sample_index=next_sample_index, samples=320)
        peer.interrupt_response()
        fence = peer._fence
        assert fence is not None
        assert fence.deadline - fence.started_at == pytest.approx(0.03)

        await _consume_queued_capture(peer, samples=320)
        await _consume_capture(
            peer,
            sample_index=next_sample_index + 320,
            samples=3_840,
        )
        assert fence.capture_complete

        time.sleep(0.06)  # noqa: ASYNC251 - deliberately stall the event loop.
        peer._maybe_complete_fence()

        assert emitted == [
            (
                "lifecycle",
                {"event_type": "capture.rtp_started", "generation": 0},
            ),
            ("fatal", "media_fence_timeout"),
        ]
        assert fence.timed_out
        assert peer._muted
        assert not _has_fenced(emitted)
    finally:
        await peer.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_event", ["media.quiet", "interrupt.fenced"])
async def test_peer_lifecycle_emission_failure_seals_fence_until_timeout(
    monkeypatch: pytest.MonkeyPatch,
    failed_event: str,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    monkeypatch.setattr(peer_module, "MEDIA_QUIET_SECONDS", 1.0)
    _use_fast_fence(monkeypatch, guard=0.005, quiet=0.005, timeout=0.05)
    attempted: list[str] = []

    def emit_lifecycle(value: dict[str, str | int]) -> None:
        event_type = value["event_type"]
        assert isinstance(event_type, str)
        attempted.append(event_type)
        if event_type == failed_event:
            raise RuntimeError("simulated IPC emission failure")

    peer, emitted = _fake_device_peer(
        monkeypatch,
        emit_lifecycle=emit_lifecycle,
    )
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        track.frames.put_nowait(_remote_frame(20, pts=0))
        await _wait_for(lambda: "media.started" in attempted)
        next_sample_index = await _prime_capture_sender(peer)
        _feed_capture(peer, sample_index=next_sample_index, samples=320)
        peer.interrupt_response()
        await _consume_queued_capture(peer, samples=320)
        await _consume_capture(
            peer,
            sample_index=next_sample_index + 320,
            samples=3_840,
        )

        await _wait_for(lambda: ("fatal", "lifecycle_output_failed") in emitted)
        fence = peer._fence
        assert fence is not None and fence.lifecycle_failed
        assert peer._muted
        assert peer._failed
        assert peer._fence_timeout_timer is None
        if failed_event == "media.quiet":
            assert "interrupt.fenced" not in attempted

        await asyncio.sleep(0.06)
        assert [value for value in emitted if value[0] == "fatal"] == [
            ("fatal", "lifecycle_output_failed")
        ]
        assert peer._fence is fence
        assert not fence.timed_out
        assert peer._muted
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_successful_fenced_write_commits_before_deadline_overrun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fast_fence(monkeypatch, guard=0.005, quiet=0.005, timeout=0.05)
    emitted: list[Any] = []

    def slow_fenced_write(value: dict[str, str | int]) -> None:
        emitted.append(("lifecycle", value))
        if value["event_type"] == "interrupt.fenced":
            time.sleep(0.06)

    peer, fatals = _fake_device_peer(
        monkeypatch,
        emit_lifecycle=slow_fenced_write,
    )
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        next_sample_index = await _prime_capture_sender(peer)
        _feed_capture(peer, sample_index=next_sample_index, samples=320)
        peer.interrupt_response()
        await _consume_queued_capture(peer, samples=320)
        await _consume_capture(
            peer,
            sample_index=next_sample_index + 320,
            samples=3_840,
        )

        await _wait_for(lambda: _has_fenced(emitted))
        await asyncio.sleep(0)
        assert peer._fence is None
        assert not peer._muted
        assert not any(kind == "fatal" for kind, _value in fatals)
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_latched_fatal_wins_over_already_awakened_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    _use_fast_fence(monkeypatch, guard=0.0, quiet=0.005, timeout=0.20)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    barrier_entered = asyncio.Event()
    release_barrier = asyncio.Event()

    async def controlled_barrier(*, not_before: float) -> None:
        assert not_before > 0
        barrier_entered.set()
        await release_barrier.wait()

    peer._receiver_drain_barrier = controlled_barrier  # type: ignore[method-assign]
    try:
        next_sample_index = await _prime_capture_sender(peer)
        _feed_capture(peer, sample_index=next_sample_index, samples=320)
        peer.interrupt_response()
        await _consume_queued_capture(peer, samples=320)
        await _consume_capture(
            peer,
            sample_index=next_sample_index + 320,
            samples=3_840,
        )
        await _wait_for(barrier_entered.is_set)

        # Wake completion first, then latch terminal failure before its task is
        # scheduled. The old ordering could subsequently unmute the same peer.
        release_barrier.set()
        peer._safe_fatal("connection_failed")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert emitted == [
            (
                "lifecycle",
                {"event_type": "capture.rtp_started", "generation": 0},
            ),
            ("fatal", "connection_failed"),
        ]
        assert peer._failed
        assert peer._muted
        assert not _has_fenced(emitted)

        track.frames.put_nowait(_remote_frame(28, pts=0))
        await _wait_for(lambda: track.received == 1)
        await asyncio.sleep(0)
        assert not any(kind == "playback" for kind, _value in emitted)
        assert peer._muted
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_media_started_emission_failure_never_leaks_playback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)

    def reject_lifecycle(_value: dict[str, str | int]) -> None:
        raise RuntimeError("simulated IPC emission failure")

    peer, emitted = _fake_device_peer(
        monkeypatch,
        emit_lifecycle=reject_lifecycle,
    )
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        track.frames.put_nowait(_remote_frame(24, pts=0))
        await _wait_for(lambda: bool(emitted))

        assert emitted == [("fatal", "lifecycle_output_failed")]
        assert peer._muted
        assert not peer._media_generation_open
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_counts_receiver_activity_before_buffering_resampler_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", BufferingResampler)
    _use_fast_fence(monkeypatch, guard=0.005, quiet=0.05, timeout=0.30)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        next_sample_index = await _prime_capture_sender(peer)
        _feed_capture(peer, sample_index=next_sample_index, samples=320)
        peer.interrupt_response()
        await _consume_queued_capture(peer, samples=320)
        await _consume_capture(
            peer,
            sample_index=next_sample_index + 320,
            samples=3_840,
        )

        await asyncio.sleep(0.03)
        track.frames.put_nowait(_remote_frame(21, pts=0))
        await _wait_for(lambda: track.received == 1)
        await asyncio.sleep(0.03)
        assert not _has_fenced(emitted)
        assert not any(kind == "playback" for kind, _value in emitted)

        await _wait_for(lambda: _has_fenced(emitted))
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_fence_discards_pre_fence_resampler_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", OneFrameDelayResampler)
    _use_fast_fence(monkeypatch, guard=0.005, quiet=0.005, timeout=0.20)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        track.frames.put_nowait(_remote_frame(29, pts=0))
        await _wait_for(lambda: track.frames.quiet_and_drained(quiet_seconds=0.0))
        assert not any(kind == "playback" for kind, _value in emitted)

        next_sample_index = await _prime_capture_sender(peer)
        _feed_capture(peer, sample_index=next_sample_index, samples=320)
        peer.interrupt_response()
        await _consume_queued_capture(peer, samples=320)
        await _consume_capture(
            peer,
            sample_index=next_sample_index + 320,
            samples=3_840,
        )
        await _wait_for(lambda: _has_fenced(emitted))

        track.frames.put_nowait(_remote_frame(30, pts=480))
        await _wait_for(lambda: track.frames.quiet_and_drained(quiet_seconds=0.0))
        assert not any(kind == "playback" for kind, _value in emitted)

        track.frames.put_nowait(_remote_frame(31, pts=960))
        await _wait_for(lambda: any(kind == "playback" for kind, _value in emitted))
        assert [value.pcm for kind, value in emitted if kind == "playback"] == [
            b"\x1e\x00" * 480
        ]
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_fence_replaces_retained_pinned_jitter_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fast_fence(monkeypatch, guard=0.005, quiet=0.005, timeout=0.20)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    receiver = peer._audio_receiver
    assert receiver is not None
    original = receiver._RTCRtpReceiver__jitter_buffer
    original._origin = 7
    original._packets[7] = object()
    try:
        next_sample_index = await _prime_capture_sender(peer)
        _feed_capture(peer, sample_index=next_sample_index, samples=320)
        peer.interrupt_response()
        await _consume_queued_capture(peer, samples=320)
        await _consume_capture(
            peer,
            sample_index=next_sample_index + 320,
            samples=3_840,
        )
        await _wait_for(lambda: _has_fenced(emitted))

        replacement = receiver._RTCRtpReceiver__jitter_buffer
        assert replacement is not original
        assert peer._is_supported_empty_jitter_buffer(replacement)
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_receiver_drain_barrier_processes_queued_ready_rtp_while_muted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    monkeypatch.setattr(peer_module, "MEDIA_QUIET_SECONDS", 0.20)
    _use_fast_fence(monkeypatch, guard=0.0, quiet=0.02, timeout=0.20)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()

    class AdversarialBarrierQueue(asyncio.Queue[Any]):
        injected = False

        def put_nowait(self, item: Any) -> None:
            super().put_nowait(item)
            if not self.injected:
                self.injected = True
                # Reproduce aiortc's worker-thread -> run_coroutine_threadsafe
                # scheduling depth. The decoded frame becomes ready multiple
                # turns after the fence requested its receiver proof.
                asyncio.get_running_loop().call_soon(
                    lambda: asyncio.get_running_loop().call_soon(
                        asyncio.create_task,
                        track.frames.put(_remote_frame(22, pts=0)),
                    )
                )

    peer._receiver_barriers = AdversarialBarrierQueue()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        next_sample_index = await _prime_capture_sender(peer)
        _feed_capture(peer, sample_index=next_sample_index, samples=320)
        peer.interrupt_response()
        await _consume_queued_capture(peer, samples=320)
        _feed_capture(
            peer,
            sample_index=next_sample_index + 320,
            samples=3_840,
        )

        await _consume_queued_capture(peer, samples=3_840)
        assert peer._muted
        assert not _has_fenced(emitted)

        await _wait_for(lambda: track.received == 1)
        assert not any(kind == "playback" for kind, _value in emitted)
        assert not _has_fenced(emitted)

        await _wait_for(lambda: _has_fenced(emitted))
        track.frames.put_nowait(_remote_frame(23, pts=480))
        await _wait_for(lambda: any(kind == "playback" for kind, _value in emitted))
        playback = [value for kind, value in emitted if kind == "playback"]
        assert [value.pcm for value in playback] == [b"\x17\x00" * 480]
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_decoder_thread_production_during_loop_stall_resets_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    monkeypatch.setattr(peer_module, "MEDIA_QUIET_SECONDS", 0.20)
    _use_fast_fence(monkeypatch, guard=0.0, quiet=0.02, timeout=0.20)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    barrier_requested = threading.Event()

    class ObservedBarrierQueue(asyncio.Queue[Any]):
        def put_nowait(self, item: Any) -> None:
            super().put_nowait(item)
            barrier_requested.set()

    peer._receiver_barriers = ObservedBarrierQueue()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    worker: threading.Thread | None = None
    try:
        next_sample_index = await _prime_capture_sender(peer)
        _feed_capture(peer, sample_index=next_sample_index, samples=320)
        peer.interrupt_response()
        await _consume_queued_capture(peer, samples=320)
        await _consume_capture(
            peer,
            sample_index=next_sample_index + 320,
            samples=3_840,
        )
        await _wait_for(barrier_requested.is_set)

        loop = asyncio.get_running_loop()

        def decoder_worker_put() -> None:
            time.sleep(0.005)
            future = asyncio.run_coroutine_threadsafe(
                track.frames.put(_remote_frame(25, pts=0)),
                loop,
            )
            future.result(timeout=1.0)

        worker = threading.Thread(target=decoder_worker_put, daemon=True)
        worker.start()
        time.sleep(0.05)  # noqa: ASYNC251 - reproduce a stalled sidecar loop.

        await _wait_for(lambda: track.received == 1)
        await asyncio.sleep(0)
        assert not any(kind == "playback" for kind, _value in emitted)
        assert not _has_fenced(emitted)

        await _wait_for(lambda: _has_fenced(emitted))
        track.frames.put_nowait(_remote_frame(26, pts=480))
        await _wait_for(lambda: any(kind == "playback" for kind, _value in emitted))
        assert [value.pcm for kind, value in emitted if kind == "playback"] == [
            b"\x1a\x00" * 480
        ]
    finally:
        if worker is not None:
            await asyncio.to_thread(worker.join, 1.0)
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_decoder_thread_terminal_during_loop_stall_never_fences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    _use_fast_fence(monkeypatch, guard=0.0, quiet=0.02, timeout=0.20)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    barrier_requested = threading.Event()

    class ObservedBarrierQueue(asyncio.Queue[Any]):
        def put_nowait(self, item: Any) -> None:
            super().put_nowait(item)
            barrier_requested.set()

    peer._receiver_barriers = ObservedBarrierQueue()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    peer._consumer_tasks.add(task)
    task.add_done_callback(peer._consumer_done)
    worker: threading.Thread | None = None
    try:
        next_sample_index = await _prime_capture_sender(peer)
        _feed_capture(peer, sample_index=next_sample_index, samples=320)
        peer.interrupt_response()
        await _consume_queued_capture(peer, samples=320)
        await _consume_capture(
            peer,
            sample_index=next_sample_index + 320,
            samples=3_840,
        )
        await _wait_for(barrier_requested.is_set)

        loop = asyncio.get_running_loop()

        def decoder_worker_end() -> None:
            time.sleep(0.005)
            future = asyncio.run_coroutine_threadsafe(track.frames.put(None), loop)
            future.result(timeout=1.0)

        worker = threading.Thread(target=decoder_worker_end, daemon=True)
        worker.start()
        time.sleep(0.05)  # noqa: ASYNC251 - reproduce a stalled sidecar loop.

        await _wait_for(lambda: ("fatal", "remote_audio_failed") in emitted)
        assert peer._failed
        assert peer._muted
        assert not _has_fenced(emitted)
    finally:
        if worker is not None:
            await asyncio.to_thread(worker.join, 1.0)
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_terminal_decoder_queue_blocks_fence_until_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fast_fence(monkeypatch, guard=0.0, quiet=0.005, timeout=0.05)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        decoder_queue = peer._decoder_queue
        assert decoder_queue is not None
        decoder_queue.put_nowait(None)

        next_sample_index = await _prime_capture_sender(peer)
        _feed_capture(peer, sample_index=next_sample_index, samples=320)
        peer.interrupt_response()
        await _consume_queued_capture(peer, samples=320)
        await _consume_capture(
            peer,
            sample_index=next_sample_index + 320,
            samples=3_840,
        )

        await _wait_for(lambda: ("fatal", "media_fence_timeout") in emitted)
        assert peer._failed
        assert peer._muted
        assert not _has_fenced(emitted)
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_inflight_decoder_work_must_finish_before_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    _use_fast_fence(monkeypatch, guard=0.0, quiet=0.01, timeout=0.30)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    decoder_queue = peer._decoder_queue
    assert decoder_queue is not None
    decoder_started = threading.Event()
    release_decoder = threading.Event()
    worker_errors: list[BaseException] = []
    loop = asyncio.get_running_loop()

    def decoder_worker() -> None:
        try:
            decoder_queue.put_nowait("encoded-old-response")
            assert decoder_queue.get() == "encoded-old-response"
            decoder_started.set()
            assert release_decoder.wait(1.0)
            future = asyncio.run_coroutine_threadsafe(
                track.frames.put(_remote_frame(32, pts=0)),
                loop,
            )
            future.result(timeout=1.0)
            assert decoder_queue.get() is None
        except BaseException as error:  # noqa: BLE001 - thread test boundary.
            worker_errors.append(error)

    worker = threading.Thread(target=decoder_worker, daemon=True)
    worker.start()
    try:
        assert decoder_started.wait(1.0)
        next_sample_index = await _prime_capture_sender(peer)
        _feed_capture(peer, sample_index=next_sample_index, samples=320)
        peer.interrupt_response()
        await _consume_queued_capture(peer, samples=320)
        await _consume_capture(
            peer,
            sample_index=next_sample_index + 320,
            samples=3_840,
        )

        await asyncio.sleep(0.04)
        assert not _has_fenced(emitted)

        release_decoder.set()
        await _wait_for(lambda: track.received == 1)
        assert not any(kind == "playback" for kind, _value in emitted)
        await _wait_for(lambda: _has_fenced(emitted))
    finally:
        release_decoder.set()
        decoder_queue.put_nowait(None)
        await asyncio.to_thread(worker.join, 1.0)
        await _close_audio_peer(peer, task)
    assert not worker.is_alive()
    assert not worker_errors


@pytest.mark.asyncio
async def test_peer_capture_timeout_when_progress_stops_before_token_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fast_fence(monkeypatch, guard=0.005, quiet=0.005, timeout=0.05)
    peer, emitted = _fake_device_peer(monkeypatch)
    try:
        # The token may legitimately arrive before aiortc's first sender pull.
        # The first queued sample is then the sender cursor, not an unavailable
        # proof, while the full pre-token queue remains the watermark.
        next_sample_index = 8_000
        _feed_capture(peer, sample_index=next_sample_index, samples=3_840)
        _feed_capture(peer, sample_index=next_sample_index + 3_840, samples=1_920)
        peer.interrupt_response()
        fence = peer._fence
        assert fence is not None
        assert fence.required_consumed_end == next_sample_index + 5_760

        await _consume_queued_capture(peer, samples=3_840)
        assert peer.input_track.consumed_sample_end == next_sample_index + 3_840
        assert not fence.capture_complete
        await _wait_for(lambda: ("fatal", "media_fence_capture_timeout") in emitted)

        assert not _has_fenced(emitted)
        assert fence.timed_out
        assert peer._muted
    finally:
        await peer.stop()


@pytest.mark.asyncio
async def test_peer_local_interrupt_requires_guard_quiet_and_capture_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fast_fence(monkeypatch)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        next_sample_index = await _prime_capture_sender(peer)
        _feed_capture(peer, sample_index=next_sample_index, samples=2_240)
        peer.data_channel.readyState = "closed"
        peer.interrupt_response()
        assert peer.data_channel.sent == []

        await asyncio.sleep(0.04)
        assert not _has_fenced(emitted)

        # Provider lifecycle is observational only on the live Frameless
        # surface and cannot replace actual outbound microphone progress.
        _provider_event(peer, type="input_audio_buffer.speech_started")
        await _consume_queued_capture(peer, samples=2_240)
        assert peer.input_track.consumed_samples == 2_560
        assert not _has_fenced(emitted)

        await _consume_capture(
            peer,
            sample_index=next_sample_index + 2_240,
            samples=2_240,
        )
        assert peer.input_track.consumed_samples == 4_800
        await _wait_for(lambda: _has_fenced(emitted))
        assert [
            value["event_type"] for kind, value in emitted if kind == "lifecycle"
        ] == [
            "capture.rtp_started",
            "input_audio_buffer.speech_started",
            "interrupt.fenced",
        ]
        assert not any(kind == "fatal" for kind, _ in emitted)
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_interrupt_requires_fresh_guard_after_preexisting_rtp_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    monkeypatch.setattr(peer_module, "MEDIA_QUIET_SECONDS", 0.005)
    _use_fast_fence(monkeypatch, guard=0.04, quiet=0.015)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        track.frames.put_nowait(_remote_frame(10, pts=0))
        await _wait_for(
            lambda: any(
                kind == "lifecycle" and value["event_type"] == "media.quiet"
                for kind, value in emitted
            )
        )
        emitted.clear()

        next_sample_index = await _prime_capture_sender(peer)
        _feed_capture(peer, sample_index=next_sample_index, samples=320)
        started_at = asyncio.get_running_loop().time()
        peer.interrupt_response()
        await _consume_queued_capture(peer, samples=320)
        await _consume_capture(
            peer,
            sample_index=next_sample_index + 320,
            samples=3_840,
        )
        await asyncio.sleep(0.025)
        assert not _has_fenced(emitted)

        await _wait_for(lambda: _has_fenced(emitted))
        assert asyncio.get_running_loop().time() - started_at >= 0.035
        assert emitted == [
            (
                "lifecycle",
                {"event_type": "capture.rtp_started", "generation": 1},
            ),
            (
                "lifecycle",
                {"event_type": "interrupt.fenced", "generation": 1},
            ),
        ]
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_late_rtp_resets_fence_quiet_and_preserves_media_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    monkeypatch.setattr(peer_module, "MEDIA_QUIET_SECONDS", 0.20)
    _use_fast_fence(monkeypatch, guard=0.02, quiet=0.03)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        track.frames.put_nowait(_remote_frame(10, pts=0))
        await _wait_for(lambda: any(kind == "playback" for kind, _value in emitted))
        emitted.clear()

        next_sample_index = await _prime_capture_sender(peer)
        _feed_capture(peer, sample_index=next_sample_index, samples=320)
        peer.interrupt_response()
        await _consume_queued_capture(peer, samples=320)
        await _consume_capture(
            peer,
            sample_index=next_sample_index + 320,
            samples=3_840,
        )
        await asyncio.sleep(0.015)
        track.frames.put_nowait(_remote_frame(11, pts=480))
        await _wait_for(lambda: track.received == 2)
        await asyncio.sleep(0.02)
        assert not _has_fenced(emitted)
        assert not any(kind == "playback" for kind, _ in emitted)

        await _wait_for(lambda: _has_fenced(emitted))
        track.frames.put_nowait(_remote_frame(12, pts=960))
        await _wait_for(lambda: any(kind == "playback" for kind, _value in emitted))

        assert [
            value["event_type"] if kind == "lifecycle" else "playback"
            for kind, value in emitted
        ] == [
            "capture.rtp_started",
            "media.quiet",
            "interrupt.fenced",
            "media.started",
            "playback",
        ]
        assert [value for kind, value in emitted if kind == "playback"] == [
            PlaybackAudio(
                generation=2,
                sample_index=480,
                media_timestamp=960,
                pcm=b"\x0c\x00" * 480,
            )
        ]
        assert peer.data_channel.sent == []
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("frame_value", [0, 15])
async def test_peer_continuous_rtp_hits_fixed_timeout_and_stays_muted(
    monkeypatch: pytest.MonkeyPatch,
    frame_value: int,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    monkeypatch.setattr(peer_module, "MEDIA_QUIET_SECONDS", 0.01)
    _use_fast_fence(monkeypatch, guard=0.01, quiet=0.02, timeout=0.05)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        next_sample_index = await _prime_capture_sender(peer)
        _feed_capture(peer, sample_index=next_sample_index, samples=320)
        peer.interrupt_response()
        await _consume_queued_capture(peer, samples=320)
        await _consume_capture(
            peer,
            sample_index=next_sample_index + 320,
            samples=3_840,
        )
        deadline = asyncio.get_running_loop().time() + 0.07
        pts = 0
        while asyncio.get_running_loop().time() < deadline:
            track.frames.put_nowait(_remote_frame(frame_value, pts=pts))
            pts += 480
            await asyncio.sleep(0.005)

        await _wait_for(lambda: ("fatal", "media_fence_timeout") in emitted)
        await asyncio.sleep(0.025)
        assert not _has_fenced(emitted)
        assert not any(kind == "playback" for kind, _ in emitted)
        assert peer._muted
        assert peer._fence is not None and peer._fence.timed_out
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_duplicate_interrupt_does_not_restart_fence_or_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fast_fence(monkeypatch, guard=0.20, quiet=0.20, timeout=0.04)
    peer, emitted = _fake_device_peer(monkeypatch)
    try:
        next_sample_index = await _prime_capture_sender(peer)
        _feed_capture(peer, sample_index=next_sample_index, samples=320)
        peer.interrupt_response()
        fence = peer._fence
        quiet_timer = peer._fence_quiet_timer
        timeout_timer = peer._fence_timeout_timer
        await asyncio.sleep(0.02)

        peer.interrupt_response()
        assert peer._fence is fence
        assert peer._fence_quiet_timer is quiet_timer
        assert peer._fence_timeout_timer is timeout_timer
        assert peer.data_channel.sent == []

        await _wait_for(lambda: ("fatal", "media_fence_capture_timeout") in emitted)
        assert emitted == [
            (
                "lifecycle",
                {"event_type": "capture.rtp_started", "generation": 0},
            ),
            ("fatal", "media_fence_capture_timeout"),
        ]
    finally:
        await peer.stop()


@pytest.mark.asyncio
async def test_peer_provider_speech_started_is_informational_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_module, "AudioResampler", PassthroughResampler)
    monkeypatch.setattr(peer_module, "MEDIA_QUIET_SECONDS", 0.20)
    peer, emitted = _fake_device_peer(monkeypatch)
    track = QueuedRemoteTrack()
    task = asyncio.create_task(peer._consume_remote_audio(track))
    try:
        _provider_event(peer, type="input_audio_buffer.speech_started")
        track.frames.put_nowait(_remote_frame(13, pts=0))
        await _wait_for(lambda: any(kind == "playback" for kind, _value in emitted))

        assert peer._fence is None
        assert not peer._muted
        assert [
            value["event_type"] if kind == "lifecycle" else "playback"
            for kind, value in emitted
        ] == [
            "input_audio_buffer.speech_started",
            "playback.rtp_started",
            "media.started",
            "playback",
        ]
        assert not _has_fenced(emitted)
    finally:
        await _close_audio_peer(peer, task)


@pytest.mark.asyncio
async def test_peer_stop_cancels_every_fence_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_fast_fence(monkeypatch, guard=0.02, quiet=0.02, timeout=0.03)
    peer, emitted = _fake_device_peer(monkeypatch)
    peer.interrupt_response()
    quiet_timer = peer._fence_quiet_timer
    timeout_timer = peer._fence_timeout_timer
    assert quiet_timer is not None
    assert timeout_timer is not None

    await peer.stop()
    assert peer._media_quiet_timer is None
    assert peer._fence_quiet_timer is None
    assert peer._fence_timeout_timer is None
    assert quiet_timer.cancelled()
    assert timeout_timer.cancelled()
    await asyncio.sleep(0.04)
    assert emitted == []
