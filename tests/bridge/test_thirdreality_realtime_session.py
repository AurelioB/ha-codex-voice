from __future__ import annotations

import json
import struct
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from device.thirdreality.realtime_client import session as session_module
from device.thirdreality.realtime_client.config import (
    DEFAULT_AEC_SINK_VOLUME_CEILING_PERCENT,
    DEFAULT_PLAYBACK_VOLUME_PERCENT,
    DEFAULT_PULSE_AEC_METHOD,
    DEFAULT_PULSE_AEC_SINK,
    DEFAULT_PULSE_AEC_SOURCE,
    DEVICE_WEBRTC_TRANSPORT,
    NATIVE_AEC3_CAPTURE,
    PULSEAUDIO_AEC_CAPTURE,
    RealtimeConfig,
)
from device.thirdreality.realtime_client.session import (
    _PACTL_ARGV,
    _PAPLAY_ARGV,
    RealtimeSession,
    SessionState,
    SubmitResult,
    _AudioPacer,
    _AudioPacket,
    _direct_answer_sdp,
    _direct_rollover_answer_sdp,
    _direct_rollover_context_retained,
    _DirectSessionDiagnostics,
    _EchoDecision,
    _EchoDecisionKind,
    _pcm_has_local_barge_in_signal,
    _pcm_has_signal,
    _PcmPlayer,
    _PlaybackAttenuator,
    _RenderEchoGuard,
    _validate_direct_started,
    _validate_started,
    _verify_pulseaudio_aec,
)
from device.thirdreality.realtime_client.sidecar import SidecarError
from device.thirdreality.realtime_client.websocket import Message, WebSocketError
from device.thirdreality.webrtc_sidecar.protocol import ControlMessage, PlaybackAudio


@pytest.fixture(autouse=True)
def _silence_device_syslog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_module.syslog, "syslog", lambda *_args: None)


def _config(**overrides: object) -> RealtimeConfig:
    values: dict[str, Any] = {
        "url": "ws://192.0.2.10:8787/v1/realtime",
        "connect_address": "192.0.2.10",
        "token": "secret-token",
        "wake_phrase": "okay computer",
        "connect_timeout_seconds": 1.0,
        "handshake_timeout_seconds": 2.0,
        "io_timeout_seconds": 1.0,
        "idle_timeout_seconds": 10.0,
        "max_session_seconds": 30.0,
        "ping_interval_seconds": 5.0,
        "pong_timeout_seconds": 2.0,
        "input_queue_bytes": 8_192,
        "fallback_buffer_bytes": 4_096,
        "output_queue_bytes": 8_192,
        "max_message_bytes": 4_096,
        "full_duplex": False,
        "voice": None,
        "prompt": None,
        "pulse_aec_source": None,
        "pulse_aec_sink": None,
        "pulse_aec_method": None,
        "aec_sink_volume_ceiling_percent": (DEFAULT_AEC_SINK_VOLUME_CEILING_PERCENT),
        "playback_volume_percent": DEFAULT_PLAYBACK_VOLUME_PERCENT,
    }
    values.update(overrides)
    return RealtimeConfig(**values)


def _duplex_config(**overrides: object) -> RealtimeConfig:
    return _config(
        full_duplex=True,
        pulse_aec_source=DEFAULT_PULSE_AEC_SOURCE,
        pulse_aec_sink=DEFAULT_PULSE_AEC_SINK,
        **overrides,
    )


class _FakeStdin:
    def __init__(self) -> None:
        self.closed = False

    def fileno(self) -> int:
        return 91

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.waited = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float) -> int:
        assert timeout > 0
        self.waited += 1
        if self.returncode is None:
            return 0
        return self.returncode


class _RecordingPlayer:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []
        self._active = False

    def begin(self, epoch: int) -> None:
        self._active = True
        self.events.append(("begin", epoch))

    def resume(self, epoch: int) -> None:
        self._active = True
        self.events.append(("resume", epoch))

    def prepare(self) -> None:
        self.events.append(("prepare", None))

    def enqueue(self, value: bytes) -> None:
        self.events.append(("audio", value))

    def finish(self, epoch: int) -> None:
        self.events.append(("finish", epoch))

    def abort(self) -> None:
        self._active = False
        self.events.append(("abort", None))

    @property
    def active(self) -> bool:
        return self._active


class _LoopPlayer:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []
        self.allow_drain = threading.Event()
        self._active = False
        self._finishing = False

    def begin(self, epoch: int) -> None:
        self._active = True
        self.events.append(("begin", epoch))

    def resume(self, epoch: int) -> None:
        self._active = True
        self.events.append(("resume", epoch))

    def prepare(self) -> None:
        self.events.append(("prepare", None))

    def enqueue(self, value: bytes) -> None:
        self.events.append(("audio", value))

    def finish(self, epoch: int) -> None:
        self._finishing = True
        self.events.append(("finish", epoch))

    def service(self) -> None:
        if self._finishing and self.allow_drain.is_set():
            self._active = False
            self._finishing = False

    def abort(self) -> None:
        self._active = False
        self._finishing = False
        self.events.append(("abort", None))

    @property
    def active(self) -> bool:
        return self._active


class _DirectRecordingPlayer(_LoopPlayer):
    def finish(self, epoch: int) -> None:
        self.events.append(("finish", epoch))
        self._active = False


class _BlockingServicePlayer(_DirectRecordingPlayer):
    def __init__(self) -> None:
        super().__init__()
        self.block_service = threading.Event()
        self.service_entered = threading.Event()
        self.release_service = threading.Event()
        self.service_calls = 0

    def service(self) -> None:
        self.service_calls += 1
        if self.block_service.is_set():
            self.service_entered.set()
            assert self.release_service.wait(1.0)
        super().service()


class _FakeSidecar:
    def __init__(self) -> None:
        self.process = _FakeProcess()
        self._incoming: deque[ControlMessage | PlaybackAudio] = deque()
        self._condition = threading.Condition()
        self.answers: list[str] = []
        self.audio: list[tuple[bytes, int, int]] = []
        self.capture_commits = 0
        self.ipc_sent: list[tuple[str, bytes | None]] = []
        self.interruptions = 0
        self.offer_requests = 0
        self.offer_capture_gains: list[float] = []
        self.standby_offer_requests = 0
        self.standby_offer_capture_gains: list[float] = []
        self.standby_promotions: list[int] = []
        self.peer_epoch = 1
        self.audio_by_peer_epoch: dict[int, list[tuple[bytes, int, int]]] = {1: []}
        self.stop_count = 0
        self.fail_stop = False
        self.fail_standby_offer = False
        self.close_timeouts: list[float] = []
        self.closed = False

    def wait_readable(self, timeout: float) -> bool:
        with self._condition:
            self._condition.wait_for(
                lambda: bool(self._incoming) or self.closed,
                timeout=max(0.0, timeout),
            )
            return bool(self._incoming)

    def feed(self, message: ControlMessage | PlaybackAudio) -> None:
        with self._condition:
            self._incoming.append(message)
            self._condition.notify_all()

    def request_offer(self, *, direct_capture_gain_db: float = 0.0) -> None:
        self.offer_requests += 1
        self.offer_capture_gains.append(direct_capture_gain_db)
        self.feed(
            ControlMessage(
                "offer",
                {
                    "sdp": (
                        "v=0\r\n"
                        "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
                        "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
                    )
                },
            )
        )

    def set_answer(self, sdp: str) -> None:
        self.answers.append(sdp)
        self.feed(ControlMessage("answer.applied", {}))
        self.feed(ControlMessage("connected", {}))
        self.feed(ControlMessage("data.ready", {}))

    def request_standby_offer(
        self,
        *,
        direct_capture_gain_db: float = 0.0,
    ) -> None:
        if self.fail_standby_offer:
            raise SidecarError("simulated standby offer failure")
        self.standby_offer_requests += 1
        self.standby_offer_capture_gains.append(direct_capture_gain_db)
        self.feed(
            ControlMessage(
                "standby.offer",
                {
                    "sdp": (
                        "v=0\r\n"
                        "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
                        "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
                    ),
                    "peer_epoch": self.peer_epoch + 1,
                },
            )
        )

    def promote_standby(self, peer_epoch: int) -> None:
        if self.fail_stop:
            raise SidecarError("simulated sidecar promotion failure")
        self.standby_promotions.append(peer_epoch)
        self.peer_epoch = peer_epoch
        self.audio_by_peer_epoch.setdefault(peer_epoch, [])
        self.feed(ControlMessage("standby.promoted", {"peer_epoch": peer_epoch}))

    def send_audio(
        self,
        pcm: bytes,
        *,
        sample_index: int,
        capture_monotonic_ns: int,
    ) -> None:
        packet = (pcm, sample_index, capture_monotonic_ns)
        self.audio.append(packet)
        self.audio_by_peer_epoch.setdefault(self.peer_epoch, []).append(packet)
        self.ipc_sent.append(("audio", pcm))

    def commit_capture(self) -> None:
        self.capture_commits += 1
        self.ipc_sent.append(("capture.commit", None))
        self.feed(ControlMessage("capture.ready", {}))

    def drain_messages(
        self,
        *,
        maximum: int = 64,
    ) -> list[ControlMessage | PlaybackAudio]:
        with self._condition:
            return [
                self._incoming.popleft()
                for _ in range(min(maximum, len(self._incoming)))
            ]

    def interrupt_response(self) -> None:
        self.interruptions += 1
        self.ipc_sent.append(("interrupt", None))

    def stop(self) -> None:
        self.stop_count += 1
        if self.fail_stop:
            raise SidecarError("simulated sidecar stop failure")
        self.feed(ControlMessage("stopped", {}))

    def close(self, *, timeout: float = 1.0) -> None:
        self.close_timeouts.append(timeout)
        with self._condition:
            self.closed = True
            self._condition.notify_all()


class _BlockingDrainSidecar(_FakeSidecar):
    def __init__(self) -> None:
        super().__init__()
        self.block_drain = threading.Event()
        self.drain_entered = threading.Event()
        self.release_drain = threading.Event()
        self.drain_calls = 0

    def drain_messages(
        self,
        *,
        maximum: int = 64,
    ) -> list[ControlMessage | PlaybackAudio]:
        self.drain_calls += 1
        if self.block_drain.is_set():
            self.drain_entered.set()
            assert self.release_drain.wait(1.0)
        return super().drain_messages(maximum=maximum)


class _DeferredCaptureReadySidecar(_FakeSidecar):
    def __init__(self) -> None:
        super().__init__()
        self.capture_commit_received = threading.Event()

    def commit_capture(self) -> None:
        self.capture_commits += 1
        self.ipc_sent.append(("capture.commit", None))
        self.capture_commit_received.set()

    def acknowledge_capture_ready(self) -> None:
        self.feed(ControlMessage("capture.ready", {}))


class _FakeRealtimeConnection:
    def __init__(self) -> None:
        self.json_sent: list[dict[str, object]] = []
        self.binary_sent: list[tuple[bytes, float]] = []
        self.pings: list[bytes] = []
        self.close_frames = 0
        self.closed = False
        self.fail_close = False
        self._incoming: deque[Message] = deque()
        self._condition = threading.Condition()

    @property
    def transport(self) -> _FakeRealtimeConnection:
        return self

    def feed(self, message: Message) -> None:
        with self._condition:
            self._incoming.append(message)
            self._condition.notify_all()

    def wait_readable(self, timeout: float) -> bool:
        with self._condition:
            self._condition.wait_for(
                lambda: bool(self._incoming) or self.closed,
                timeout=max(0.0, timeout),
            )
            return bool(self._incoming) or self.closed

    def send_json(self, value: dict[str, object]) -> None:
        with self._condition:
            self.json_sent.append(value)
            self._condition.notify_all()

    def send_binary(self, value: bytes) -> None:
        with self._condition:
            self.binary_sent.append((value, time.monotonic()))
            self._condition.notify_all()

    def send_ping(self, payload: bytes) -> None:
        with self._condition:
            self.pings.append(payload)
            self._condition.notify_all()

    def send_close(self) -> None:
        with self._condition:
            self.close_frames += 1
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self.closed = True
            self._condition.notify_all()
        if self.fail_close:
            raise OSError("simulated close failure")

    def receive_message(self) -> Message:
        with self._condition:
            if not self._incoming:
                if self.closed:
                    raise WebSocketError("simulated peer disconnect")
                raise AssertionError("receive_message called without readable input")
            return self._incoming.popleft()

    def wait_for_json(self, value: dict[str, object], timeout: float = 1.0) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: value in self.json_sent,
                timeout=timeout,
            )

    def wait_for_binary_count(self, count: int, timeout: float = 1.0) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: len(self.binary_sent) >= count,
                timeout=timeout,
            )


def _wait_for(predicate: Any, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def _render_features(count: int, *, seed: int = 0x13579BDF) -> list[int]:
    """Return deterministic, centered speech-like features without fixtures."""
    state = seed
    values: list[int] = []
    for _ in range(count):
        state ^= (state << 13) & 0xFFFFFFFF
        state ^= state >> 17
        state ^= (state << 5) & 0xFFFFFFFF
        values.append(((state & 0xFFFF) - 32_768) // 6)
    return values


def _expanded_pcm(
    features: list[int] | tuple[int, ...],
    factor: int,
    *,
    numerator: int = 1,
    denominator: int = 1,
    offset: int = 0,
) -> bytes:
    samples = [
        sample * numerator // denominator + offset
        for sample in features
        for _ in range(factor)
    ]
    return struct.pack(f"<{len(samples)}h", *samples)


def _guard_capture(
    features: list[int],
    *,
    start: int,
    delay_ms: int = 160,
    numerator: int = 1,
    denominator: int = 2,
    offset: int = 700,
) -> tuple[bytes, float]:
    frame = features[start : start + 256]
    captured_at = (
        session_module._RENDER_ECHO_NOMINAL_PLAYOUT_SECONDS
        + start / session_module._RENDER_ECHO_FEATURE_RATE
        + delay_ms / 1_000
        + len(frame) / session_module._RENDER_ECHO_FEATURE_RATE
    )
    return (
        _expanded_pcm(
            frame,
            session_module._RENDER_ECHO_CAPTURE_DOWNSAMPLE,
            numerator=numerator,
            denominator=denominator,
            offset=offset,
        ),
        captured_at,
    )


def _calibrated_render_echo_guard() -> tuple[_RenderEchoGuard, list[int]]:
    guard = _RenderEchoGuard()
    features = _render_features(3_200)
    guard.begin_epoch(1, reset=True)
    guard.observe_render(
        _expanded_pcm(
            features,
            session_module._RENDER_ECHO_RENDER_DOWNSAMPLE,
        ),
        written_at=0.0,
    )
    for start in (400, 656, 912):
        capture, captured_at = _guard_capture(features, start=start)
        decision = guard.classify(
            capture,
            captured_at=captured_at,
            output_epoch=1,
            calibrating=True,
        )
        assert decision is not None
        assert decision.kind is _EchoDecisionKind.ECHO
    return guard, features


def _force_render_matched_near_end(session: RealtimeSession) -> None:
    """Give lifecycle-focused bridge tests affirmative near-end evidence."""
    guard = session._render_echo_guard
    assert guard is not None
    guard.classify = (  # type: ignore[method-assign]
        lambda *_args, output_epoch, **_kwargs: _EchoDecision(
            _EchoDecisionKind.NEAR_END,
            output_epoch,
            correlation_permille=200,
            reference_matched=True,
            interrupt_qualified=True,
        )
    )


def test_shutdown_closes_idle_prewarmed_sidecar_and_blocks_rewarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecars = (_FakeSidecar(),)
    monkeypatch.setattr(
        session_module,
        "_PREWARMED_SIDECARS",
        deque(sidecars),
    )
    monkeypatch.setattr(
        session_module,
        "_GLOBAL_SIDECAR_PROCESSES",
        {id(sidecar): sidecar for sidecar in sidecars},
    )
    monkeypatch.setattr(session_module, "_SHUTTING_DOWN", False)

    session_module.shutdown_all_sessions(timeout=0.0)

    assert all(sidecar.closed for sidecar in sidecars)
    assert not session_module._PREWARMED_SIDECARS
    assert session_module.prewarm_device_webrtc() is False
    with pytest.raises(SidecarError, match="shutting down"):
        session_module._take_prewarmed_sidecar()


def test_prewarm_keeps_exactly_one_device_webrtc_worker_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = (_FakeSidecar(),)
    pending = deque(created)

    def launch() -> _FakeSidecar:
        return pending.popleft()

    monkeypatch.setattr(session_module, "_PREWARMED_SIDECARS", deque())
    monkeypatch.setattr(session_module, "_GLOBAL_SIDECAR_PROCESSES", {})
    monkeypatch.setattr(session_module, "_SHUTTING_DOWN", False)
    monkeypatch.setattr(
        session_module.WebRtcSidecarClient,
        "launch",
        staticmethod(launch),
    )

    assert session_module.prewarm_device_webrtc() is True
    assert tuple(session_module._PREWARMED_SIDECARS) == created
    assert session_module._take_prewarmed_sidecar() is created[0]
    assert not session_module._PREWARMED_SIDECARS


def test_global_sidecar_admission_waits_for_actual_process_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closing = (_FakeSidecar(),)
    launched = _FakeSidecar()
    launch_calls: list[None] = []
    monkeypatch.setattr(session_module, "_PREWARMED_SIDECARS", deque())
    monkeypatch.setattr(
        session_module,
        "_GLOBAL_SIDECAR_PROCESSES",
        {id(sidecar): sidecar for sidecar in closing},
    )
    monkeypatch.setattr(session_module, "_SHUTTING_DOWN", False)
    monkeypatch.setattr(session_module, "_SIDECAR_SLOT_WAIT_SECONDS", 0.5)

    def launch() -> _FakeSidecar:
        launch_calls.append(None)
        return launched

    monkeypatch.setattr(
        session_module.WebRtcSidecarClient,
        "launch",
        staticmethod(launch),
    )

    def release_slot() -> None:
        time.sleep(0.03)
        closing[0].process.returncode = 0

    releaser = threading.Thread(target=release_slot)
    releaser.start()
    selected = session_module._take_prewarmed_sidecar()
    releaser.join()

    assert selected is launched
    assert launch_calls == [None]
    assert len(session_module._GLOBAL_SIDECAR_PROCESSES) == 1


def test_global_sidecar_admission_times_out_without_launching_second_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occupied = (_FakeSidecar(),)
    monkeypatch.setattr(session_module, "_PREWARMED_SIDECARS", deque())
    monkeypatch.setattr(
        session_module,
        "_GLOBAL_SIDECAR_PROCESSES",
        {id(sidecar): sidecar for sidecar in occupied},
    )
    monkeypatch.setattr(session_module, "_SHUTTING_DOWN", False)
    monkeypatch.setattr(session_module, "_SIDECAR_SLOT_WAIT_SECONDS", 0.03)
    monkeypatch.setattr(
        session_module.WebRtcSidecarClient,
        "launch",
        staticmethod(lambda: pytest.fail("a second child must not launch")),
    )

    with pytest.raises(SidecarError, match="slots are occupied"):
        session_module._take_prewarmed_sidecar()

    assert len(session_module._GLOBAL_SIDECAR_PROCESSES) == 1


def test_direct_terminal_waits_until_global_sidecar_replenishment_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replenishing = threading.Event()
    release_replenishment = threading.Event()

    def prewarm() -> bool:
        replenishing.set()
        assert release_replenishment.wait(1.0)
        return True

    monkeypatch.setattr(session_module, "prewarm_device_webrtc", prewarm)
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        aec_verifier=lambda _config: None,
        volume_guard=lambda _config: None,
        direct_player_factory=lambda _maximum, _sink: _DirectRecordingPlayer(),
    )
    session._sidecar_factory = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        SidecarError("startup failed")
    )
    with session._state_lock:
        session._state = SessionState.CONNECTING
    worker = threading.Thread(target=session._run_device_webrtc)
    worker.start()

    assert replenishing.wait(1.0)
    assert session.state is SessionState.CONNECTING
    assert not session.terminal

    release_replenishment.set()
    worker.join(1.0)
    assert not worker.is_alive()
    assert session.state is SessionState.FAILED
    assert session.terminal


def test_direct_terminal_still_publishes_when_replenishment_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session_module,
        "prewarm_device_webrtc",
        lambda: (_ for _ in ()).throw(RuntimeError("replenishment failed")),
    )
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        aec_verifier=lambda _config: None,
        volume_guard=lambda _config: None,
        direct_player_factory=lambda _maximum, _sink: _DirectRecordingPlayer(),
    )
    session._sidecar_factory = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
        SidecarError("startup failed")
    )
    with session._state_lock:
        session._state = SessionState.CONNECTING

    session._run_device_webrtc()

    assert session.state is SessionState.FAILED
    assert session.terminal


def _install_fake_loop_io(
    monkeypatch: pytest.MonkeyPatch,
    player: _LoopPlayer,
) -> None:
    monkeypatch.setattr(
        session_module,
        "_socket_readable",
        lambda transport, timeout: transport.wait_readable(timeout),
    )
    monkeypatch.setattr(
        session_module,
        "_PcmPlayer",
        lambda *_args, **_kwargs: player,
    )


def _started(*, remote_cancel: bool = False) -> dict[str, object]:
    return {
        "type": "started",
        "protocol_version": 2,
        "conversation_mode": "native",
        "audio_transport": "binary",
        "input_sample_rate": 16_000,
        "input_channels": 1,
        "output_sample_rate": 24_000,
        "output_channels": 1,
        "capabilities": {
            "binary_pcm16": True,
            "local_flush": True,
            "remote_cancel": remote_cancel,
            "same_session_interrupt_ack": True,
            "server_owned_media": True,
            "native_end_conversation": True,
        },
    }


def _direct_started() -> dict[str, object]:
    return {
        "type": "started",
        "version": "v3",
        "protocol_version": 3,
        "conversation_mode": "native",
        "transport": "webrtc",
        "audio_over_bridge": False,
        "sideband_control": True,
    }


def _start_direct_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_first_standby: bool = False,
    sidecar_created: Callable[[list[_FakeSidecar]], None] | None = None,
    sidecar_builder: Callable[[], _FakeSidecar] = _FakeSidecar,
    direct_player: _DirectRecordingPlayer | None = None,
    clock: Callable[[], float] = time.monotonic,
    aec_verifier: Callable[[RealtimeConfig], None] | None = None,
    realtime_connection: _FakeRealtimeConnection | None = None,
    notify_live_capture_opened: bool = True,
    before_direct_answer: Callable[
        [RealtimeSession, _FakeSidecar, _FakeRealtimeConnection], None
    ]
    | None = None,
    **config_overrides: object,
) -> tuple[
    RealtimeSession,
    _FakeRealtimeConnection,
    _FakeSidecar,
    _DirectRecordingPlayer,
    list[_FakeSidecar],
]:
    # Rollover tests exercise post-settle barge-in. The dedicated convergence
    # test below owns the non-zero physical playback guard boundary.
    monkeypatch.setattr(
        session_module,
        "_LOCAL_BARGE_IN_PLAYBACK_SETTLE_SECONDS",
        0.0,
    )
    connection = realtime_connection or _FakeRealtimeConnection()
    sidecars: list[_FakeSidecar] = []
    factory_calls = 0

    def make_sidecar() -> _FakeSidecar:
        nonlocal factory_calls
        factory_calls += 1
        sidecar = sidecar_builder()
        if fail_first_standby and factory_calls == 1:
            sidecar.fail_standby_offer = True
        sidecars.append(sidecar)
        if sidecar_created is not None:
            sidecar_created(sidecars)
        return sidecar

    player = direct_player or _DirectRecordingPlayer()
    monkeypatch.setattr(
        session_module,
        "_socket_readable",
        lambda transport, timeout: transport.wait_readable(timeout),
    )
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            **config_overrides,
        ),
        clock=clock,
        connection_factory=lambda **_kwargs: connection,  # type: ignore[arg-type]
        aec_verifier=aec_verifier or (lambda _config: None),
        volume_guard=lambda _config: None,
        sidecar_factory=make_sidecar,  # type: ignore[arg-type]
        direct_player_factory=lambda _maximum, _sink: player,
    )
    session.start()
    assert _wait_for(lambda: bool(connection.json_sent))
    sidecar = sidecars[0]
    start = connection.json_sent[0]
    assert start["protocol_version"] == 3
    assert start["transport"] == {
        "type": "webrtc",
        "sdp": (
            "v=0\r\n"
            "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
            "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
        ),
    }
    if before_direct_answer is not None:
        before_direct_answer(session, sidecar, connection)
    connection.feed(
        Message(
            "text",
            json.dumps(
                {
                    "type": "answer",
                    "protocol_version": 3,
                    "transport": {
                        "type": "webrtc",
                        "sdp": (
                            "v=0\r\n"
                            "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
                            "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
                        ),
                    },
                }
            ),
        )
    )
    assert connection.wait_for_json({"type": "transport_ready", "protocol_version": 3})
    connection.feed(Message("text", json.dumps(_direct_started())))
    assert _wait_for(lambda: session.ready)
    if notify_live_capture_opened:
        session.notify_live_capture_opened()
    assert sidecars == [sidecar]
    assert factory_calls == 1
    return session, connection, sidecar, player, sidecars


def test_initial_standby_waits_for_live_capture_open_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _connection, sidecar, _player, sidecars = _start_direct_session(
        monkeypatch,
        notify_live_capture_opened=False,
    )

    try:
        assert session.ready
        assert sidecars == [sidecar]
        assert sidecar.standby_offer_requests == 0

        session.notify_live_capture_opened()

        assert _wait_for(lambda: sidecar.standby_offer_requests == 1)
        session.notify_live_capture_opened()
        time.sleep(0.02)
        assert sidecar.standby_offer_requests == 1
    finally:
        session.stop()
        assert session.join(1.0)


def test_direct_handshake_budget_starts_after_local_aec_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [10.0]

    def prepare_aec(_config: RealtimeConfig) -> None:
        # Device pactl verification and sink preparation are local preflight,
        # not WebRTC signaling. They must not consume the configured network
        # handshake budget, while max_session_seconds still spans both phases.
        now[0] += 4.0

    session, _connection, _sidecar, player, _sidecars = _start_direct_session(
        monkeypatch,
        handshake_timeout_seconds=2.0,
        clock=lambda: now[0],
        aec_verifier=prepare_aec,
    )

    assert session.ready
    assert ("prepare", None) in player.events
    session.stop()
    assert session.join(1.0)


def test_initial_transport_ready_waits_for_ordered_capture_commit_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeRealtimeConnection()
    initial = _DeferredCaptureReadySidecar()
    sidecar_count = 0
    prefix = (b"\x01\x00" * 320, b"\x02\x00" * 640)
    post_commit = b"\x03\x00" * 320
    release_errors: list[BaseException] = []
    session_holder: list[RealtimeSession] = []

    def build_sidecar() -> _FakeSidecar:
        nonlocal sidecar_count
        sidecar_count += 1
        return initial if sidecar_count == 1 else _FakeSidecar()

    def queue_startup(
        session: RealtimeSession,
        _sidecar: _FakeSidecar,
        _connection: _FakeRealtimeConnection,
    ) -> None:
        session_holder.append(session)
        assert all(
            session.submit_audio(value) is SubmitResult.ACCEPTED for value in prefix
        )

    def release_ready() -> None:
        try:
            assert initial.capture_commit_received.wait(1.0)
            assert not connection.wait_for_json(
                {"type": "transport_ready", "protocol_version": 3},
                timeout=0.05,
            )
            assert session_holder[0].submit_audio(post_commit) is SubmitResult.ACCEPTED
            assert _wait_for(lambda: len(initial.audio) == 3)
            initial.acknowledge_capture_ready()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test thread.
            release_errors.append(exc)
            initial.acknowledge_capture_ready()

    release_thread = threading.Thread(target=release_ready)
    release_thread.start()
    session: RealtimeSession | None = None
    try:
        session, _connection, sidecar, _player, _sidecars = _start_direct_session(
            monkeypatch,
            realtime_connection=connection,
            sidecar_builder=build_sidecar,
            before_direct_answer=queue_startup,
        )
        release_thread.join(timeout=1.0)

        assert not release_thread.is_alive()
        assert release_errors == []
        assert sidecar is initial
        assert [packet[1] for packet in initial.audio] == [0, 320, 960]
        assert [kind for kind, _value in initial.ipc_sent] == [
            "audio",
            "audio",
            "capture.commit",
            "audio",
        ]
        assert initial.capture_commits == 1
    finally:
        initial.acknowledge_capture_ready()
        release_thread.join(timeout=1.0)
        if session is not None:
            session.stop()
            assert session.join(1.0)


def test_direct_player_restores_sink_volume_before_complete_aec_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    player = _DirectRecordingPlayer()

    def verify_after_volume_restore(_config: RealtimeConfig) -> None:
        assert player.events == [("prepare", None)]

    session, _connection, _sidecar, _player, _sidecars = _start_direct_session(
        monkeypatch,
        direct_player=player,
        aec_verifier=verify_after_volume_restore,
    )

    assert session.ready
    session.stop()
    assert session.join(1.0)


def test_direct_webrtc_negotiates_on_device_and_never_relays_pcm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, connection, sidecar, _player, sidecars = _start_direct_session(
        monkeypatch,
        direct_capture_gain_db=6.0,
    )
    frame = b"\x01\x00" * 1_024

    assert session.submit_audio(frame) is SubmitResult.ACCEPTED
    assert _wait_for(lambda: bool(sidecar.audio))
    sent_pcm, sample_index, captured_ns = sidecar.audio[0]
    assert sent_pcm == frame
    assert sample_index == 0
    assert captured_ns > 0
    assert not connection.binary_sent
    assert all(item.offer_capture_gains == [6.0] for item in sidecars)

    session.stop()
    assert session.join(1.0)
    assert connection.json_sent.count({"type": "stop"}) == 1
    assert sidecar.stop_count >= 1
    assert connection.closed


def test_direct_capture_signal_does_not_extend_idle_timeout_but_lifecycle_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MutableClock:
        def __init__(self) -> None:
            self.value = 10.0
            self.calls = 0

        def __call__(self) -> float:
            self.calls += 1
            return self.value

    clock = MutableClock()
    session, _connection, sidecar, _player, _sidecars = _start_direct_session(
        monkeypatch,
        clock=clock,
        idle_timeout_seconds=5.0,
        max_session_seconds=100.0,
        ping_interval_seconds=60.0,
    )
    diagnostics = session._direct_diagnostics
    assert diagnostics is not None
    signal_capture = (500).to_bytes(2, "little", signed=True) * 4

    for expected_packets, timestamp in enumerate(range(11, 15), start=1):
        clock.value = float(timestamp)
        assert session.submit_audio(signal_capture) is SubmitResult.ACCEPTED
        session._wake_network.set()
        assert _wait_for(
            lambda expected_packets=expected_packets: (
                len(sidecar.audio) == expected_packets
            )
        )

    assert diagnostics.capture_signal_frames == 4
    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "response.created", "generation": 0},
        )
    )
    assert _wait_for(lambda: diagnostics.lifecycle_events.get("response.created") == 1)
    calls_after_observation = clock.calls
    assert _wait_for(lambda: clock.calls > calls_after_observation)

    for expected_packets, timestamp in enumerate(range(15, 19), start=5):
        clock.value = float(timestamp)
        assert session.submit_audio(signal_capture) is SubmitResult.ACCEPTED
        session._wake_network.set()
        assert _wait_for(
            lambda expected_packets=expected_packets: (
                len(sidecar.audio) == expected_packets
            )
        )

    # Provider lifecycle at t=14 extended the initial t=10 deadline, while
    # signal-bearing local capture through t=18 did not extend it again.
    assert session.state is SessionState.READY
    clock.value = 19.0
    session._wake_network.set()
    assert session.join(1.0)
    assert session.state is SessionState.FAILED
    assert diagnostics.capture_signal_frames == 8


def test_direct_silent_playback_and_noop_session_events_do_not_extend_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [10.0]
    session, _connection, sidecar, player, _sidecars = _start_direct_session(
        monkeypatch,
        clock=lambda: now[0],
        idle_timeout_seconds=5.0,
        max_session_seconds=100.0,
        ping_interval_seconds=60.0,
    )
    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: ("begin", 1) in player.events)

    for index, timestamp in enumerate(range(11, 15)):
        now[0] = float(timestamp)
        sidecar.feed(
            PlaybackAudio(
                generation=1,
                sample_index=index * 4,
                media_timestamp=index * 4,
                pcm=bytes(8),
            )
        )
        sidecar.feed(
            ControlMessage(
                "lifecycle",
                {"event_type": "session.updated", "generation": 1},
            )
        )
        assert _wait_for(
            lambda expected=index + 1: (
                sum(event[0] == "audio" for event in player.events) == expected
            )
        )

    # Media start at t=10 is semantic. Neither decoded RTP silence nor the
    # recurring session bookkeeping events may move its t=15 idle deadline.
    now[0] = 15.0
    session._wake_network.set()
    assert session.join(1.0)
    assert session.state is SessionState.FAILED


def test_direct_session_logs_content_free_capture_and_playback_metrics(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("INFO", logger="linux_voice_assistant.realtime"):
        session, _connection, sidecar, player, _sidecars = _start_direct_session(
            monkeypatch
        )
        quiet_capture = (100).to_bytes(2, "little", signed=True) * 4
        signal_capture = (500).to_bytes(2, "little", signed=True) * 4

        assert session.submit_audio(quiet_capture) is SubmitResult.ACCEPTED
        assert _wait_for(lambda: len(sidecar.audio) == 1)
        assert session.submit_audio(signal_capture) is SubmitResult.ACCEPTED
        assert _wait_for(lambda: len(sidecar.audio) == 2)

        quiet_playback = (100).to_bytes(2, "little", signed=True) * 4
        signal_playback = (300).to_bytes(2, "little", signed=True) * 4
        peaked_playback = (1000).to_bytes(2, "little", signed=True) + b"\x00\x00" * 3
        sidecar.feed(
            ControlMessage(
                "lifecycle",
                {"event_type": "media.started", "generation": 1},
            )
        )
        for sample_index, pcm in enumerate(
            (quiet_playback, signal_playback, peaked_playback)
        ):
            sidecar.feed(
                PlaybackAudio(
                    generation=1,
                    sample_index=sample_index * 4,
                    media_timestamp=sample_index * 4,
                    pcm=pcm,
                )
            )
        sidecar.feed(
            ControlMessage(
                "lifecycle",
                {"event_type": "media.quiet", "generation": 1},
            )
        )
        assert _wait_for(
            lambda: sum(event[0] == "audio" for event in player.events) == 3
        )

        session.stop()
        assert session.join(1.0)

    summaries = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "INFO"
        and record.getMessage().startswith(
            "ThirdReality direct WebRTC session summary:"
        )
    ]
    assert len(summaries) == 1
    summary = summaries[0]
    for expected in (
        "handshake_ready=yes",
        "peer_answer_applied=yes",
        "peer_connected=yes",
        "peer_data_ready=yes",
        "capture_sent_packets=2",
        "capture_sent_bytes=16",
        "capture_max_peak=500",
        "capture_max_rms=500",
        "capture_signal_frames=1",
        "lifecycle_events=media.quiet:1,media.started:1",
        "playback_signal_packets=2",
        "playback_signal_bytes=16",
        "playback_max_peak=1000",
        "playback_max_rms=500",
        "outcome=stopped",
    ):
        assert expected in summary


def test_direct_startup_failure_logs_only_phase_and_exception_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive = "secret-token transcript v=0 private-session-id"

    def fail_preflight(_config: RealtimeConfig) -> None:
        raise WebSocketError(sensitive)

    def unexpected_sidecar() -> _FakeSidecar:
        raise AssertionError("preflight failure must not launch a sidecar")

    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        aec_verifier=fail_preflight,
        sidecar_factory=unexpected_sidecar,  # type: ignore[arg-type]
    )

    with caplog.at_level("INFO", logger="linux_voice_assistant.realtime"):
        session.start()
        assert session.join(1.0)

    assert session.state is SessionState.FAILED
    assert session.failed_before_ready
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING"
    ]
    expected_warning = (
        "ThirdReality direct WebRTC session failed: phase=preflight "
        "error=WebSocketError"
    )
    assert warnings == [expected_warning]
    summaries = [
        record.getMessage() for record in caplog.records if record.levelname == "INFO"
    ]
    assert len(summaries) == 1
    assert "handshake_ready=no phase=preflight" in summaries[0]
    assert "peer_answer_applied=no" in summaries[0]
    assert "peer_connected=no" in summaries[0]
    assert "peer_data_ready=no" in summaries[0]
    assert "outcome=failed" in summaries[0]
    assert sensitive not in caplog.text


def test_direct_summary_sanitizes_lifecycle_types_and_never_logs_content(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive = "TOP-SECRET-TRANSCRIPT-AND-IDENTIFIER"
    with caplog.at_level("INFO", logger="linux_voice_assistant.realtime"):
        session, _connection, sidecar, _player, _sidecars = _start_direct_session(
            monkeypatch
        )
        sidecar.feed(
            ControlMessage(
                "lifecycle",
                {
                    "event_type": "session.updated",
                    "generation": 0,
                    "transcript": sensitive,
                    "response_id": sensitive,
                    "token": sensitive,
                },
            )
        )
        sidecar.feed(
            ControlMessage(
                "lifecycle",
                {
                    "event_type": f"session.{sensitive}",
                    "generation": 0,
                },
            )
        )
        assert _wait_for(lambda: not sidecar._incoming)

        session.stop()
        assert session.join(1.0)

    summaries = [
        record.getMessage() for record in caplog.records if record.levelname == "INFO"
    ]
    assert len(summaries) == 1
    assert "lifecycle_events=other:1,session.updated:1" in summaries[0]
    assert sensitive not in caplog.text
    assert "secret-token" not in caplog.text


def test_direct_syslog_reports_ready_waiting_output_and_terminal_aggregates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [10.0]
    records: list[tuple[int, str]] = []
    monkeypatch.setattr(
        session_module.syslog,
        "syslog",
        lambda priority, message: records.append((priority, message)),
    )
    session, _connection, sidecar, player, _sidecars = _start_direct_session(
        monkeypatch,
        clock=lambda: now[0],
        idle_timeout_seconds=120.0,
        max_session_seconds=900.0,
        ping_interval_seconds=60.0,
    )

    assert _wait_for(
        lambda: (
            sum(
                "direct_webrtc_status=ready" in message
                for _priority, message in records
            )
            == 8
        )
    )
    ready_records = [
        message
        for priority, message in records
        if priority == session_module.syslog.LOG_INFO
        and "direct_webrtc_status=ready" in message
    ]
    assert len(ready_records) == 8
    assert {
        next(field for field in message.split() if field.startswith("record="))
        for message in ready_records
    } == {
        "record=state",
        "record=media",
        "record=levels",
        "record=gain",
        "record=echo",
        "record=transport",
        "record=events_1",
        "record=events_2",
    }
    ready = next(message for message in ready_records if "record=state" in message)
    assert ready.startswith("codex-voice direct_webrtc_status=ready ")
    assert "phase=runtime" in ready
    assert "handshake_ready=yes" in ready
    assert "peer_answer_applied=yes" in ready
    assert "peer_connected=yes" in ready
    assert "peer_data_ready=yes" in ready
    assert "outcome=live" in ready

    sensitive = "TOP-SECRET-TRANSCRIPT-AND-IDENTIFIER"
    capture = (500).to_bytes(2, "little", signed=True) * 4
    assert session.submit_audio(capture) is SubmitResult.ACCEPTED
    transport_events = (
        "capture.direction.sendrecv",
        "capture.outbound_active",
    )
    decisive_events = (
        "capture.rtp_started",
        "playback.rtp_started",
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "response.created",
        "response.done",
        "turn.created",
        "turn.done",
        "output_audio_buffer.started",
        "output_audio_buffer.stopped",
    )
    for event_type in (*transport_events, *decisive_events):
        sidecar.feed(
            ControlMessage(
                "lifecycle",
                {"event_type": event_type, "generation": 0},
            )
        )
    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {
                "event_type": f"session.{sensitive}",
                "generation": 0,
                "transcript": sensitive,
                "token": sensitive,
                "sdp": sensitive,
            },
        )
    )
    sidecar.feed(
        ControlMessage(
            "capture.metrics",
            {
                "post_gain_max_peak": 997,
                "post_gain_max_rms": 500,
                "clipped_samples": 3,
                "clipped_frames": 1,
            },
        )
    )
    assert _wait_for(lambda: len(sidecar.audio) == 1 and not sidecar._incoming)

    now[0] += session_module._DIRECT_SYSLOG_INTERVAL_SECONDS
    session._wake_network.set()
    assert _wait_for(
        lambda: any(
            "direct_webrtc_status=waiting_output" in item[1] for item in records
        )
    )
    heartbeat_records = [
        message
        for _priority, message in records
        if "direct_webrtc_status=waiting_output" in message
    ]
    assert len(heartbeat_records) == 8
    heartbeat_media = next(
        message for message in heartbeat_records if "record=media" in message
    )
    heartbeat_levels = next(
        message for message in heartbeat_records if "record=levels" in message
    )
    heartbeat_gain = next(
        message for message in heartbeat_records if "record=gain" in message
    )
    heartbeat_transport = next(
        message for message in heartbeat_records if "record=transport" in message
    )
    assert "capture_sent_packets=1" in heartbeat_media
    assert "capture_signal_frames=1" in heartbeat_media
    assert "playback_signal_packets=0" in heartbeat_media
    assert "capture_max_peak=500" in heartbeat_levels
    assert "capture_max_rms=500" in heartbeat_levels
    assert "playback_max_peak=0" in heartbeat_levels
    assert "playback_max_rms=0" in heartbeat_levels
    assert "post_gain_max_peak=997" in heartbeat_gain
    assert "post_gain_max_rms=500" in heartbeat_gain
    assert "clipped_samples=3" in heartbeat_gain
    assert "clipped_frames=1" in heartbeat_gain
    assert "direction_sendrecv=1" in heartbeat_transport
    assert "outbound_active=1" in heartbeat_transport
    heartbeat_events = "\n".join(
        message for message in heartbeat_records if "record=events_" in message
    )
    for event_type in decisive_events:
        assert f"{event_type}=1" in heartbeat_events

    playback = (300).to_bytes(2, "little", signed=True) * 4
    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    sidecar.feed(
        PlaybackAudio(
            generation=1,
            sample_index=0,
            media_timestamp=0,
            pcm=playback,
        )
    )
    assert _wait_for(lambda: ("audio", playback) in player.events)
    heartbeat_count = sum(
        "direct_webrtc_status=waiting_output" in item[1] for item in records
    )

    now[0] += session_module._DIRECT_SYSLOG_INTERVAL_SECONDS * 3
    session._wake_network.set()
    time.sleep(0.05)
    assert (
        sum("direct_webrtc_status=waiting_output" in item[1] for item in records)
        == heartbeat_count
    )

    session.stop()
    assert session.join(1.0)
    terminals = [
        message
        for _priority, message in records
        if "direct_webrtc_status=terminal" in message
    ]
    assert len(terminals) == 8
    terminal_state = next(message for message in terminals if "record=state" in message)
    terminal_media = next(message for message in terminals if "record=media" in message)
    terminal_levels = next(
        message for message in terminals if "record=levels" in message
    )
    terminal_events = "\n".join(
        message for message in terminals if "record=events_" in message
    )
    assert "capture.rtp_started=1" in terminal_events
    assert "playback.rtp_started=1" in terminal_events
    assert "playback_signal_packets=1" in terminal_media
    assert "capture_max_peak=500" in terminal_levels
    assert "capture_max_rms=500" in terminal_levels
    assert "playback_max_peak=300" in terminal_levels
    assert "playback_max_rms=300" in terminal_levels
    assert "outcome=stopped" in terminal_state
    emitted = "\n".join(message for _priority, message in records)
    assert sensitive not in emitted
    assert "secret-token" not in emitted
    assert "sdp=v=0" not in emitted
    assert all(
        message.isascii()
        and len(message.encode("ascii"))
        <= session_module._DIRECT_SYSLOG_RECORD_MAX_BYTES
        for _priority, message in records
    )


def test_direct_syslog_failure_does_not_change_media_session_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted_records: list[tuple[str, str]] = []

    def fail_syslog(_priority: int, message: str) -> None:
        attempted_records.append((message.split()[1], message.split()[2]))
        raise OSError("syslog unavailable")

    monkeypatch.setattr(session_module.syslog, "syslog", fail_syslog)
    session, _connection, _sidecar, _player, _sidecars = _start_direct_session(
        monkeypatch
    )

    assert session.ready
    session.stop()
    assert session.join(1.0)
    assert session.state is SessionState.STOPPED
    assert attempted_records == [
        ("direct_webrtc_status=ready", "record=state"),
        ("direct_webrtc_status=ready", "record=media"),
        ("direct_webrtc_status=ready", "record=levels"),
        ("direct_webrtc_status=ready", "record=gain"),
        ("direct_webrtc_status=ready", "record=echo"),
        ("direct_webrtc_status=ready", "record=transport"),
        ("direct_webrtc_status=ready", "record=events_1"),
        ("direct_webrtc_status=ready", "record=events_2"),
        ("direct_webrtc_status=terminal", "record=state"),
        ("direct_webrtc_status=terminal", "record=media"),
        ("direct_webrtc_status=terminal", "record=levels"),
        ("direct_webrtc_status=terminal", "record=gain"),
        ("direct_webrtc_status=terminal", "record=echo"),
        ("direct_webrtc_status=terminal", "record=transport"),
        ("direct_webrtc_status=terminal", "record=events_1"),
        ("direct_webrtc_status=terminal", "record=events_2"),
    ]


def test_direct_syslog_schema_rejects_dynamic_labels_and_stays_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[str] = []
    monkeypatch.setattr(
        session_module.syslog,
        "syslog",
        lambda _priority, message: records.append(message),
    )
    diagnostics = session_module._DirectSessionDiagnostics(started_at=0.0)
    sensitive = "secret-transcript-id-error-string"
    diagnostics.phase = sensitive
    diagnostics.handshake_ready = True
    diagnostics.peer_answer_applied = True
    diagnostics.peer_connected = True
    diagnostics.peer_data_ready = True
    for event_type in session_module._DIRECT_DIAGNOSTIC_LIFECYCLE_EVENTS:
        diagnostics.lifecycle_events[event_type] = (
            session_module._DIRECT_DIAGNOSTIC_LIFECYCLE_COUNT_MAX
        )
    diagnostics.lifecycle_events["other"] = (
        session_module._DIRECT_DIAGNOSTIC_LIFECYCLE_COUNT_MAX
    )
    diagnostics.lifecycle_events[sensitive] = 10**20
    for field_name in (
        "capture_packets",
        "capture_bytes",
        "capture_max_peak",
        "capture_max_rms",
        "capture_signal_frames",
        "post_gain_max_peak",
        "post_gain_max_rms",
        "clipped_samples",
        "clipped_frames",
        "playback_signal_packets",
        "playback_signal_bytes",
        "playback_max_peak",
        "playback_max_rms",
        "echo_rejected_frames",
        "echo_near_end_frames",
        "echo_ambiguous_frames",
        "echo_max_correlation_permille",
        "echo_last_delay_ms",
    ):
        setattr(diagnostics, field_name, 10**20)

    session_module._emit_direct_syslog_status(
        diagnostics,
        status="secret-status",
        duration_ms=10**20,
        outcome="live",
    )
    session_module._emit_direct_syslog_status(
        diagnostics,
        status="waiting_output",
        duration_ms=10**20,
        outcome="secret-outcome",
    )
    session_module._emit_direct_syslog_status(
        diagnostics,
        status="waiting_output",
        duration_ms=10**20,
        outcome="remote_stopped",
    )

    assert len(records) == 8
    assert session_module._DIRECT_SYSLOG_RECORD_MAX_BYTES == 220
    assert all(
        record.isascii()
        and len(record.encode("ascii"))
        <= session_module._DIRECT_SYSLOG_RECORD_MAX_BYTES
        for record in records
    )
    assert {
        next(field for field in record.split() if field.startswith("record="))
        for record in records
    } == {
        "record=state",
        "record=media",
        "record=levels",
        "record=gain",
        "record=echo",
        "record=transport",
        "record=events_1",
        "record=events_2",
    }
    state = next(record for record in records if "record=state" in record)
    media = next(record for record in records if "record=media" in record)
    levels = next(record for record in records if "record=levels" in record)
    gain = next(record for record in records if "record=gain" in record)
    echo = next(record for record in records if "record=echo" in record)
    transport = next(record for record in records if "record=transport" in record)
    events = "\n".join(record for record in records if "record=events_" in record)
    assert "phase=unknown" in state
    assert "duration_ms=99999999" in state
    assert "outcome=remote_stopped" in state
    assert "capture_sent_packets=99999999" in media
    assert "capture_signal_frames=99999999" in media
    assert "playback_signal_packets=99999999" in media
    assert "capture_max_peak=32768" in levels
    assert "capture_max_rms=32768" in levels
    assert "playback_max_peak=32768" in levels
    assert "playback_max_rms=32768" in levels
    assert "post_gain_max_peak=32768" in gain
    assert "post_gain_max_rms=32768" in gain
    assert "clipped_samples=99999999" in gain
    assert "clipped_frames=99999999" in gain
    assert "rejected=99999999" in echo
    assert "near=99999999" in echo
    assert "ambiguous=99999999" in echo
    assert "max_corr_pm=1000" in echo
    assert "delay_ms=320" in echo
    assert "direction_sendrecv=999999" in transport
    assert "direction_sendonly=999999" in transport
    assert "direction_recvonly=999999" in transport
    assert "direction_inactive=999999" in transport
    assert "direction_unknown=999999" in transport
    assert "outbound_active=999999" in transport
    for event_type in (
        "capture.rtp_started",
        "playback.rtp_started",
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "response.created",
        "response.done",
        "turn.created",
        "turn.done",
        "output_audio_buffer.started",
        "output_audio_buffer.stopped",
    ):
        assert f"{event_type}=999999" in events
    assert sensitive not in "\n".join(records)


def test_direct_failure_record_allowlists_child_code_and_hides_unknown_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[str] = []
    monkeypatch.setattr(
        session_module.syslog,
        "syslog",
        lambda _priority, message: records.append(message),
    )
    session = RealtimeSession(_duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT))

    known = session_module._DirectSessionDiagnostics(started_at=0.0)
    session._direct_diagnostics = known
    session._observe_direct_sidecar_message(
        ControlMessage("error", {"code": "capture_rejected"})
    )
    session_module._emit_direct_syslog_status(
        known,
        status="terminal",
        duration_ms=10,
        outcome="failed",
    )

    sensitive = "private-transcript-shaped-error"
    unknown = session_module._DirectSessionDiagnostics(started_at=0.0)
    session._direct_diagnostics = unknown
    session._observe_direct_sidecar_message(
        ControlMessage("error", {"code": sensitive})
    )
    session_module._emit_direct_syslog_status(
        unknown,
        status="terminal",
        duration_ms=10,
        outcome="failed",
    )

    failures = [record for record in records if "record=failure" in record]
    assert failures == [
        (
            "codex-voice direct_webrtc_status=terminal "
            "record=failure code=capture_rejected"
        ),
        "codex-voice direct_webrtc_status=terminal record=failure code=unknown",
    ]
    assert sensitive not in "\n".join(records)


def test_direct_provider_error_lifecycle_sets_failure_code_without_followup_batch() -> (
    None
):
    session = RealtimeSession(_duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT))
    diagnostics = session_module._DirectSessionDiagnostics(started_at=0.0)
    session._direct_diagnostics = diagnostics
    for _ in range(7):
        session._observe_direct_sidecar_message(
            ControlMessage(
                "capture.metrics",
                {
                    "post_gain_max_peak": 0,
                    "post_gain_max_rms": 0,
                    "clipped_samples": 0,
                    "clipped_frames": 0,
                },
            )
        )
    session._observe_direct_sidecar_message(
        ControlMessage(
            "lifecycle",
            {"event_type": "error", "generation": 0},
        )
    )

    assert diagnostics.failure_code == "provider_error"


@pytest.mark.parametrize("boundary_name", ["stop", "interrupt"])
def test_direct_explicit_boundary_waits_for_player_service_and_blocks_later_writes(
    monkeypatch: pytest.MonkeyPatch,
    boundary_name: str,
) -> None:
    player = _BlockingServicePlayer()
    session, _connection, _sidecar, _player, _sidecars = _start_direct_session(
        monkeypatch,
        direct_player=player,
    )
    player.block_service.set()
    assert player.service_entered.wait(1.0)
    boundary_returned = threading.Event()

    def apply_boundary() -> None:
        getattr(session, boundary_name)()
        boundary_returned.set()

    boundary_thread = threading.Thread(target=apply_boundary)
    boundary_thread.start()
    try:
        assert not boundary_returned.wait(0.05)
        player.release_service.set()
        assert boundary_returned.wait(1.0)
        service_calls_at_boundary = player.service_calls
        assert session.join(1.0)
        assert player.service_calls == service_calls_at_boundary
    finally:
        player.release_service.set()
        boundary_thread.join(timeout=1.0)
        if not session.terminal:
            session.stop()
            assert session.join(1.0)


def test_direct_explicit_boundary_waits_for_output_drain_and_blocks_later_playback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _connection, active, player, _sidecars = _start_direct_session(
        monkeypatch,
        sidecar_builder=_BlockingDrainSidecar,
    )
    assert isinstance(active, _BlockingDrainSidecar)
    active.block_drain.set()
    active.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    active.feed(
        PlaybackAudio(
            generation=1,
            sample_index=0,
            media_timestamp=0,
            pcm=b"\x03\x00" * 480,
        )
    )
    assert active.drain_entered.wait(1.0)
    boundary_returned = threading.Event()

    def stop_session() -> None:
        session.stop()
        boundary_returned.set()

    boundary_thread = threading.Thread(target=stop_session)
    boundary_thread.start()
    try:
        assert not boundary_returned.wait(0.05)
        active.release_drain.set()
        assert boundary_returned.wait(1.0)
        drains_at_boundary = active.drain_calls
        audio_at_boundary = sum(event[0] == "audio" for event in player.events)
        assert session.join(1.0)
        assert active.drain_calls == drains_at_boundary
        assert sum(event[0] == "audio" for event in player.events) == audio_at_boundary
    finally:
        active.release_drain.set()
        boundary_thread.join(timeout=1.0)
        if not session.terminal:
            session.stop()
            assert session.join(1.0)


def test_direct_webrtc_barge_in_rolls_over_peer_and_replays_capture_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, connection, old_sidecar, player, sidecars = _start_direct_session(
        monkeypatch
    )
    old_sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    old_sidecar.feed(
        PlaybackAudio(
            generation=1,
            sample_index=0,
            media_timestamp=0,
            pcm=b"\x02\x00" * 480,
        )
    )
    assert _wait_for(lambda: ("audio", b"\x02\x00" * 480) in player.events)
    assert session.output_active

    before = b"\x01\x00" * 256
    speech_one = (2_000).to_bytes(2, "little", signed=True) * 512
    speech_two = (2_100).to_bytes(2, "little", signed=True) * 768
    during_handshake = b"\x02\x00" * 128
    assert session.submit_audio(before) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech_one) is SubmitResult.ACCEPTED
    assert _wait_for(lambda: len(old_sidecar.audio) == 2)
    old_timestamps = [packet[2] for packet in old_sidecar.audio]

    # The second qualifying AEC frame remains unsent when the network thread
    # observes the detector request. The fresh peer receives disjoint sent
    # preroll plus the unsent queue; the old provider may already have seen the
    # onset, but receives nothing after the cut is processed.
    assert session.submit_audio(speech_two) is SubmitResult.ACCEPTED
    assert _wait_for(lambda: session.state is SessionState.INTERRUPTING)
    assert _wait_for(lambda: ("abort", None) in player.events)
    assert not session.output_active
    assert session.ready
    assert not old_sidecar.closed
    assert _wait_for(lambda: old_sidecar.standby_promotions == [2])
    assert old_sidecar.interruptions == 0
    assert not connection.closed
    old_audio_count = len(old_sidecar.audio_by_peer_epoch[1])

    rollover = next(
        value for value in connection.json_sent if value.get("type") == "rollover"
    )
    assert rollover == {
        "type": "rollover",
        "protocol_version": 3,
        "epoch": 2,
        "transport": {
            "type": "webrtc",
            "sdp": (
                "v=0\r\n"
                "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
                "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
            ),
        },
    }
    assert session.submit_audio(during_handshake) is SubmitResult.ACCEPTED
    replacement = old_sidecar
    # Capture starts flowing through the promoted logical peer before the
    # bridge/provider answer exists, preserving original timestamp freshness.
    assert _wait_for(lambda: len(replacement.audio_by_peer_epoch[2]) == 4)
    connection.feed(
        Message(
            "text",
            json.dumps(
                {
                    "type": "rollover_answer",
                    "protocol_version": 3,
                    "epoch": 2,
                    "transport": {
                        "type": "webrtc",
                        "sdp": "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n",
                    },
                }
            ),
        )
    )
    assert connection.wait_for_json(
        {
            "type": "rollover_transport_ready",
            "protocol_version": 3,
            "epoch": 2,
        }
    )
    assert _wait_for(lambda: len(replacement.audio_by_peer_epoch[2]) == 4)
    replacement_audio = replacement.audio_by_peer_epoch[2]
    assert [packet[0] for packet in replacement_audio] == [
        before,
        speech_one,
        speech_two,
        during_handshake,
    ]
    assert [packet[1] for packet in replacement_audio] == [
        0,
        256,
        768,
        1_536,
    ]
    assert [packet[2] for packet in replacement_audio[:2]] == old_timestamps
    assert [packet[2] for packet in replacement_audio] == sorted(
        packet[2] for packet in replacement_audio
    )
    assert len(old_sidecar.audio_by_peer_epoch[1]) == old_audio_count
    assert replacement.interruptions == 0

    connection.feed(
        Message(
            "text",
            json.dumps(
                {
                    "type": "rollover_started",
                    "protocol_version": 3,
                    "epoch": 2,
                    "context_retained": True,
                }
            ),
        )
    )
    assert _wait_for(lambda: session.state is SessionState.READY)
    assert session.ready
    assert session.context_loss_rollovers == 0
    assert sidecars == [old_sidecar]
    assert _wait_for(lambda: old_sidecar.standby_offer_requests == 2)

    replacement.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    replacement.feed(
        PlaybackAudio(
            generation=1,
            sample_index=0,
            media_timestamp=0,
            pcm=b"\x04\x00" * 480,
        )
    )
    assert _wait_for(lambda: ("begin", 1) in player.events[1:])
    assert _wait_for(lambda: ("audio", b"\x04\x00" * 480) in player.events)

    session.stop()
    assert session.join(1.0)
    assert all(sidecar.closed for sidecar in sidecars)


def test_warm_rollover_transport_ready_waits_for_fresh_capture_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeRealtimeConnection()
    monkeypatch.setattr(
        session_module,
        "_socket_readable",
        lambda transport, timeout: transport.wait_readable(timeout),
    )
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.INTERRUPTING
    sidecar = _DeferredCaptureReadySidecar()
    standby = session_module._DirectStandby(
        sidecar,
        offer_sdp="v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n",
        peer_epoch=2,
    )
    player = _DirectRecordingPlayer()
    connection.feed(
        Message(
            "text",
            json.dumps(
                {
                    "type": "rollover_answer",
                    "protocol_version": 3,
                    "epoch": 2,
                    "transport": {
                        "type": "webrtc",
                        "sdp": "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n",
                    },
                }
            ),
        )
    )
    result: list[tuple[Any, ...]] = []
    errors: list[BaseException] = []

    def rollover() -> None:
        try:
            result.append(
                session._rollover_direct_peer(
                    connection,  # type: ignore[arg-type]
                    sidecar,  # type: ignore[arg-type]
                    standby,
                    player,
                    epoch=2,
                    session_deadline=time.monotonic() + 2.0,
                    capture_ages_ms=deque(),
                )
            )
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test thread.
            errors.append(exc)

    rollover_thread = threading.Thread(target=rollover)
    rollover_thread.start()
    try:
        assert sidecar.capture_commit_received.wait(1.0)
        ready = {
            "type": "rollover_transport_ready",
            "protocol_version": 3,
            "epoch": 2,
        }
        assert not connection.wait_for_json(ready, timeout=0.05)

        sidecar.acknowledge_capture_ready()
        assert connection.wait_for_json(ready)
        connection.feed(
            Message(
                "text",
                json.dumps(
                    {
                        "type": "rollover_started",
                        "protocol_version": 3,
                        "epoch": 2,
                        "context_retained": True,
                    }
                ),
            )
        )
        rollover_thread.join(timeout=1.0)

        assert not rollover_thread.is_alive()
        assert errors == []
        assert result and result[0][0] is sidecar
        assert result[0][4] is True
        assert sidecar.capture_commits == 1
        assert sidecar.standby_promotions == [2]
        assert session.state is SessionState.READY
    finally:
        sidecar.acknowledge_capture_ready()
        rollover_thread.join(timeout=1.0)


def test_direct_rollover_stop_send_failure_kills_old_peer_and_fails_before_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, connection, old_sidecar, _player, sidecars = _start_direct_session(
        monkeypatch
    )
    old_sidecar.fail_stop = True
    old_sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: session.output_active)
    speech = (2_000).to_bytes(2, "little", signed=True) * 512
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED

    assert session.join(1.0)
    assert session.state is SessionState.FAILED
    assert 0.0 in old_sidecar.close_timeouts
    assert old_sidecar.closed
    assert sidecars == [old_sidecar]
    assert not any(value.get("type") == "rollover" for value in connection.json_sent)


def test_direct_rollover_rejects_stale_bridge_epoch_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, connection, sidecar, player, sidecars = _start_direct_session(monkeypatch)
    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: session.output_active)

    speech = (2_000).to_bytes(2, "little", signed=True) * 512
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert _wait_for(lambda: ("abort", None) in player.events)
    assert _wait_for(
        lambda: any(value.get("type") == "rollover" for value in connection.json_sent)
    )
    connection.feed(
        Message(
            "text",
            json.dumps(
                {
                    "type": "rollover_answer",
                    "protocol_version": 3,
                    "epoch": 1,
                    "transport": {
                        "type": "webrtc",
                        "sdp": "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n",
                    },
                }
            ),
        )
    )
    assert session.join(1.0)
    assert session.state is SessionState.FAILED
    assert session.ready
    assert connection.closed
    assert sidecar.closed
    assert sidecars == [sidecar]
    assert sidecar.interruptions == 0


def _complete_direct_rollover(
    connection: _FakeRealtimeConnection,
    *,
    epoch: int,
    context_retained: bool,
) -> None:
    assert _wait_for(
        lambda: any(
            value.get("type") == "rollover" and value.get("epoch") == epoch
            for value in connection.json_sent
        )
    )
    connection.feed(
        Message(
            "text",
            json.dumps(
                {
                    "type": "rollover_answer",
                    "protocol_version": 3,
                    "epoch": epoch,
                    "transport": {
                        "type": "webrtc",
                        "sdp": "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n",
                    },
                }
            ),
        )
    )
    assert connection.wait_for_json(
        {
            "type": "rollover_transport_ready",
            "protocol_version": 3,
            "epoch": epoch,
        }
    )
    connection.feed(
        Message(
            "text",
            json.dumps(
                {
                    "type": "rollover_started",
                    "protocol_version": 3,
                    "epoch": epoch,
                    "context_retained": context_retained,
                }
            ),
        )
    )


def test_direct_repeated_rollovers_replenish_standby_and_keep_ready_latched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, connection, first, player, sidecars = _start_direct_session(
        monkeypatch,
        input_queue_bytes=32_768,
    )
    ready_event = session._ready
    speech = (2_000).to_bytes(2, "little", signed=True) * 1_024
    quiet = b"\x00\x00" * 1_024

    first.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: session.output_active)
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    _complete_direct_rollover(connection, epoch=2, context_retained=False)
    assert _wait_for(lambda: session.state is SessionState.READY)
    assert session.context_loss_rollovers == 1
    assert session._ready is ready_event and ready_event.is_set()
    assert sidecars == [first]
    second = first
    assert not first.closed
    assert _wait_for(lambda: first.standby_offer_requests == 2)

    second.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: session.output_active)
    # A second interruption requires a new speech edge. Eight recorder frames
    # of quiet rearm the detector after the first utterance retired its peer.
    for _ in range(8):
        assert session.submit_audio(quiet) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    _complete_direct_rollover(connection, epoch=3, context_retained=True)
    assert _wait_for(lambda: session.state is SessionState.READY)
    assert session.context_loss_rollovers == 1
    assert session._ready is ready_event and ready_event.is_set()
    assert sidecars == [first]
    assert not second.closed
    assert _wait_for(lambda: second.standby_offer_requests == 3)

    session.stop()
    assert session.join(1.0)
    assert session.state is SessionState.STOPPED
    assert all(sidecar.closed for sidecar in sidecars)
    assert player.events.count(("abort", None)) >= 3


def test_direct_barge_detector_rearms_only_after_quiet_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, connection, first, _player, sidecars = _start_direct_session(
        monkeypatch,
        input_queue_bytes=65_536,
    )
    speech = (2_000).to_bytes(2, "little", signed=True) * 1_024
    quiet = b"\x00\x00" * 1_024

    first.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: session.output_active)
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    _complete_direct_rollover(connection, epoch=2, context_retained=True)
    assert _wait_for(lambda: session.state is SessionState.READY)

    replacement = first
    replacement.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: session.output_active)

    # The same uninterrupted utterance remains above threshold after the new
    # response begins. It must not retire the replacement peer a second time.
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    time.sleep(0.05)
    assert not any(
        value.get("type") == "rollover" and value.get("epoch") == 3
        for value in connection.json_sent
    )

    # Seven production-sized recorder callbacks (448 ms) are insufficient.
    # Speech before the eighth remains part of the same local speech segment
    # and resets the quiet count.
    for _ in range(7):
        assert session.submit_audio(quiet) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    time.sleep(0.05)
    assert not any(
        value.get("type") == "rollover" and value.get("epoch") == 3
        for value in connection.json_sent
    )

    # Eight consecutive production recorder callbacks (512 ms) of quiet rearm
    # the detector. A genuinely new utterance may then interrupt normally.
    for _ in range(8):
        assert session.submit_audio(quiet) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert _wait_for(
        lambda: any(
            value.get("type") == "rollover" and value.get("epoch") == 3
            for value in connection.json_sent
        )
    )

    session.stop()
    assert session.join(1.0)
    assert all(sidecar.closed for sidecar in sidecars)


def test_direct_standby_is_created_inside_the_active_sidecar() -> None:
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        aec_verifier=lambda _config: None,
    )
    active = _FakeSidecar()
    standby = session._start_direct_standby(active)  # type: ignore[arg-type]

    assert standby is not None
    assert standby.sidecar is active
    assert active.offer_requests == 0
    assert active.standby_offer_requests == 1
    controls = active.drain_messages()
    controls, standby = session._update_direct_standby_from_controls(
        controls,  # type: ignore[arg-type]
        standby,
    )
    assert controls == []
    assert standby is not None
    assert standby.peer_epoch == 2
    assert standby.offer_sdp is not None


def test_direct_standby_failure_does_not_allocate_another_sidecar() -> None:
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        aec_verifier=lambda _config: None,
    )
    active = _FakeSidecar()
    standby = session_module._DirectStandby(active)

    controls, standby = session._update_direct_standby_from_controls(
        [ControlMessage("standby.failed", {"peer_epoch": 2})],
        standby,
    )

    assert controls == []
    assert standby is None
    assert not active.closed


def test_direct_rollover_queue_overflow_fails_closed_without_dropping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PausedPacer(_AudioPacer):
        def due(self, now: float) -> bool:
            del now
            return False

    monkeypatch.setattr(session_module, "_AudioPacer", PausedPacer)
    session, connection, sidecar, _player, sidecars = _start_direct_session(
        monkeypatch,
        input_queue_bytes=8_192,
    )
    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: session.output_active)
    speech = (2_000).to_bytes(2, "little", signed=True) * 1_024
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert _wait_for(lambda: session.state is SessionState.INTERRUPTING)
    assert _wait_for(
        lambda: any(value.get("type") == "rollover" for value in connection.json_sent)
    )
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.FULL

    assert session.join(1.0)
    assert session.state is SessionState.FAILED
    assert session.ready
    assert connection.closed
    assert all(candidate.closed for candidate in sidecars)


@pytest.mark.parametrize("request_name", ["stop", "interrupt"])
def test_direct_explicit_boundary_during_rollover_dominates_wait_and_keeps_ready_latched(
    monkeypatch: pytest.MonkeyPatch,
    request_name: str,
) -> None:
    session, connection, sidecar, _player, sidecars = _start_direct_session(monkeypatch)
    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: session.output_active)
    speech = (2_000).to_bytes(2, "little", signed=True) * 512
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert _wait_for(
        lambda: any(value.get("type") == "rollover" for value in connection.json_sent)
    )

    getattr(session, request_name)()

    assert session.join(1.0)
    assert session.state is SessionState.STOPPED
    assert session.ready
    assert connection.closed
    assert all(candidate.closed for candidate in sidecars)


def test_direct_disconnect_during_rollover_fails_closed_and_reaps_both_peers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, connection, sidecar, _player, sidecars = _start_direct_session(monkeypatch)
    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: session.output_active)
    speech = (2_000).to_bytes(2, "little", signed=True) * 512
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert _wait_for(
        lambda: any(value.get("type") == "rollover" for value in connection.json_sent)
    )

    connection.close()

    assert session.join(1.0)
    assert session.state is SessionState.FAILED
    assert session.ready
    assert all(candidate.closed for candidate in sidecars)


def test_direct_rollover_deadline_fails_closed_before_capture_can_age_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, connection, sidecar, _player, sidecars = _start_direct_session(
        monkeypatch,
        handshake_timeout_seconds=0.08,
    )
    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: session.output_active)
    speech = (2_000).to_bytes(2, "little", signed=True) * 512
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert _wait_for(
        lambda: any(value.get("type") == "rollover" for value in connection.json_sent)
    )

    assert session.join(1.0)
    assert session.state is SessionState.FAILED
    assert session.ready
    assert all(candidate.closed for candidate in sidecars)


def test_direct_rollover_fails_closed_without_offer_warm_standby(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, connection, sidecar, _player, sidecars = _start_direct_session(
        monkeypatch,
        fail_first_standby=True,
    )
    assert sidecars == [sidecar]
    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: session.output_active)
    speech = (2_000).to_bytes(2, "little", signed=True) * 512
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED

    assert session.join(1.0)
    assert session.state is SessionState.FAILED
    assert len(sidecars) == 1
    assert not any(value.get("type") == "rollover" for value in connection.json_sent)
    assert sidecar.closed


@pytest.mark.parametrize(
    "started",
    [
        {
            "type": "rollover_started",
            "protocol_version": 3,
            "epoch": 1,
            "context_retained": True,
        },
        {
            "type": "rollover_started",
            "protocol_version": 3,
            "epoch": 2,
            "context_retained": True,
            "unexpected": False,
        },
    ],
)
def test_direct_rollover_rejects_stale_or_malformed_started_message(
    monkeypatch: pytest.MonkeyPatch,
    started: dict[str, object],
) -> None:
    session, connection, sidecar, _player, sidecars = _start_direct_session(monkeypatch)
    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: session.output_active)
    speech = (2_000).to_bytes(2, "little", signed=True) * 512
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert _wait_for(
        lambda: any(value.get("type") == "rollover" for value in connection.json_sent)
    )
    connection.feed(
        Message(
            "text",
            json.dumps(
                {
                    "type": "rollover_answer",
                    "protocol_version": 3,
                    "epoch": 2,
                    "transport": {
                        "type": "webrtc",
                        "sdp": "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n",
                    },
                }
            ),
        )
    )
    assert connection.wait_for_json(
        {
            "type": "rollover_transport_ready",
            "protocol_version": 3,
            "epoch": 2,
        }
    )
    connection.feed(Message("text", json.dumps(started)))

    assert session.join(1.0)
    assert session.state is SessionState.FAILED
    assert session.ready
    assert all(candidate.closed for candidate in sidecars)


def test_direct_rollover_gates_replacement_playback_until_started_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, connection, sidecar, player, _sidecars = _start_direct_session(monkeypatch)
    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: session.output_active)
    speech = (2_000).to_bytes(2, "little", signed=True) * 512
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert _wait_for(
        lambda: any(value.get("type") == "rollover" for value in connection.json_sent)
    )
    connection.feed(
        Message(
            "text",
            json.dumps(
                {
                    "type": "rollover_answer",
                    "protocol_version": 3,
                    "epoch": 2,
                    "transport": {
                        "type": "webrtc",
                        "sdp": "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n",
                    },
                }
            ),
        )
    )
    assert connection.wait_for_json(
        {
            "type": "rollover_transport_ready",
            "protocol_version": 3,
            "epoch": 2,
        }
    )
    audible_before = sum(event[0] == "audio" for event in player.events)
    replacement = sidecar
    replacement.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    replacement.feed(
        PlaybackAudio(
            generation=1,
            sample_index=0,
            media_timestamp=0,
            pcm=b"\x07\x00" * 480,
        )
    )

    time.sleep(0.02)
    assert session.state is SessionState.INTERRUPTING
    assert sum(event[0] == "audio" for event in player.events) == audible_before
    connection.feed(
        Message(
            "text",
            json.dumps(
                {
                    "type": "rollover_started",
                    "protocol_version": 3,
                    "epoch": 2,
                    "context_retained": True,
                }
            ),
        )
    )

    assert _wait_for(lambda: session.state is SessionState.READY)
    assert _wait_for(
        lambda: (
            sum(event[0] == "audio" for event in player.events) == audible_before + 1
        )
    )
    assert ("begin", 1) in player.events
    assert ("audio", b"\x07\x00" * 480) in player.events
    session.stop()
    assert session.join(1.0)


def test_direct_rollover_fails_closed_when_pre_ack_output_exceeds_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, connection, sidecar, player, sidecars = _start_direct_session(monkeypatch)
    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: session.output_active)
    speech = (2_000).to_bytes(2, "little", signed=True) * 512
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert _wait_for(
        lambda: any(value.get("type") == "rollover" for value in connection.json_sent)
    )
    connection.feed(
        Message(
            "text",
            json.dumps(
                {
                    "type": "rollover_answer",
                    "protocol_version": 3,
                    "epoch": 2,
                    "transport": {
                        "type": "webrtc",
                        "sdp": "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n",
                    },
                }
            ),
        )
    )
    assert connection.wait_for_json(
        {
            "type": "rollover_transport_ready",
            "protocol_version": 3,
            "epoch": 2,
        }
    )
    audible_before = sum(event[0] == "audio" for event in player.events)
    replacement = sidecar
    replacement.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    replacement.feed(
        PlaybackAudio(
            generation=1,
            sample_index=0,
            media_timestamp=0,
            pcm=b"\x07\x00" * 4_096,
        )
    )

    assert session.join(1.0)
    assert session.state is SessionState.FAILED
    assert sum(event[0] == "audio" for event in player.events) == audible_before
    assert all(candidate.closed for candidate in sidecars)


def test_direct_rollover_accepts_benign_lifecycle_races_before_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, connection, sidecar, player, _sidecars = _start_direct_session(monkeypatch)
    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: session.output_active)
    speech = (2_000).to_bytes(2, "little", signed=True) * 512
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert _wait_for(
        lambda: any(value.get("type") == "rollover" for value in connection.json_sent)
    )
    replacement = sidecar
    replacement.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "session.started", "generation": 0},
        )
    )
    time.sleep(0.02)
    assert session.state is SessionState.INTERRUPTING

    connection.feed(
        Message(
            "text",
            json.dumps(
                {
                    "type": "rollover_answer",
                    "protocol_version": 3,
                    "epoch": 2,
                    "transport": {
                        "type": "webrtc",
                        "sdp": "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n",
                    },
                }
            ),
        )
    )
    assert connection.wait_for_json(
        {
            "type": "rollover_transport_ready",
            "protocol_version": 3,
            "epoch": 2,
        }
    )
    replacement.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "response.created", "generation": 0},
        )
    )
    time.sleep(0.02)
    assert session.state is SessionState.INTERRUPTING
    assert not any(event[0] == "audio" for event in player.events)

    connection.feed(
        Message(
            "text",
            json.dumps(
                {
                    "type": "rollover_started",
                    "protocol_version": 3,
                    "epoch": 2,
                    "context_retained": True,
                }
            ),
        )
    )
    assert _wait_for(lambda: session.state is SessionState.READY)
    session.stop()
    assert session.join(1.0)


@pytest.mark.parametrize("event_type", ["error", "invalid_request_error", "x_error"])
def test_direct_rollover_rejects_provider_error_lifecycle_before_started(
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
) -> None:
    session, connection, sidecar, _player, sidecars = _start_direct_session(monkeypatch)
    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: session.output_active)
    speech = (2_000).to_bytes(2, "little", signed=True) * 512
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert _wait_for(
        lambda: any(value.get("type") == "rollover" for value in connection.json_sent)
    )
    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": event_type, "generation": 0},
        )
    )

    assert session.join(1.0)
    assert session.state is SessionState.FAILED
    assert all(candidate.closed for candidate in sidecars)


def test_direct_provider_speech_started_is_informational_without_local_barge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _connection, sidecar, player, _sidecars = _start_direct_session(
        monkeypatch
    )
    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: session.output_active)

    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "input_audio_buffer.speech_started", "generation": 1},
        )
    )
    time.sleep(0.05)

    assert session.output_active
    assert session.state is SessionState.READY
    assert sidecar.interruptions == 0
    assert not any(event[0] == "abort" for event in player.events[1:])

    session.stop()
    assert session.join(1.0)


def test_direct_webrtc_explicit_interrupt_closes_for_a_fresh_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, connection, sidecar, _player, _sidecars = _start_direct_session(
        monkeypatch
    )
    sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        )
    )
    assert _wait_for(lambda: session.output_active)

    session.interrupt()

    assert session.join(1.0)
    assert session.state is SessionState.STOPPED
    assert sidecar.interruptions == 0
    assert connection.closed
    assert (
        connection.json_sent.count({"type": "transport_ready", "protocol_version": 3})
        == 1
    )
    assert connection.json_sent.count({"type": "stop"}) == 1


def test_direct_explicit_interrupt_closes_audio_admission_before_player_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _connection, sidecar, player, _sidecars = _start_direct_session(
        monkeypatch
    )
    abort_entered = threading.Event()
    release_abort = threading.Event()
    original_abort = player.abort
    first_abort = True

    def blocking_first_abort() -> None:
        nonlocal first_abort
        if first_abort:
            first_abort = False
            abort_entered.set()
            assert release_abort.wait(1.0)
        original_abort()

    player.abort = blocking_first_abort  # type: ignore[method-assign]
    session.interrupt()
    assert abort_entered.wait(1.0)

    frame = b"\x05\x00" * 1_024
    assert session.submit_audio(frame) is SubmitResult.CLOSED
    release_abort.set()

    assert session.join(1.0)
    assert not any(value[0] == frame for value in sidecar.audio)
    assert sidecar.interruptions == 0


def test_startup_audio_pacer_never_bursts_delayed_capture_blocks() -> None:
    pacer = _AudioPacer()

    assert pacer.due(10.0)
    pacer.sent(10.0, 2_048)
    assert pacer.due(10.063) is False
    assert pacer.due(10.064)

    # Even when the loop wakes late, the next block is scheduled from the
    # actual send rather than an old deadline, so it cannot catch up in a burst.
    pacer.sent(12.0, 2_048)
    assert pacer.due(12.001) is False
    assert pacer.delay(12.0) == pytest.approx(0.064)


def test_startup_audio_pacer_catches_up_at_bounded_2x_then_returns_to_1x() -> None:
    pacer = _AudioPacer()

    pacer.sent(10.0, 2_048, catching_up=True)
    assert pacer.due(10.031) is False
    assert pacer.due(10.032)
    assert pacer.delay(10.0) == pytest.approx(0.032)

    pacer.sent(10.032, 2_048, catching_up=False)
    assert pacer.due(10.095) is False
    assert pacer.due(10.096)
    assert pacer.delay(10.032) == pytest.approx(0.064)


def test_direct_capture_age_bound_fails_before_restamping_stale_pcm() -> None:
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        clock=lambda: 10.0,
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    packet = _AudioPacket(
        data=b"\x01\x00" * 320,
        captured_at=7.0,
        capture_watermark=1,
    )
    assert session._audio.put(packet)
    sidecar = _FakeSidecar()

    with pytest.raises(SidecarError, match="age bound"):
        session._send_direct_audio(
            sidecar,  # type: ignore[arg-type]
            _AudioPacer(),
            peer_epoch=1,
            sample_index=0,
            now=10.0,
            capture_ages_ms=deque(),
        )

    assert sidecar.audio == []


def test_direct_startup_capture_age_covers_bounded_negotiation_backlog() -> None:
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        clock=lambda: 10.0,
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.CONNECTING
    packet = _AudioPacket(
        data=(500).to_bytes(2, "little", signed=True) * 320,
        captured_at=(10.0 - session_module._DIRECT_STARTUP_CAPTURE_MAX_AGE_SECONDS),
        capture_watermark=1,
    )
    assert session._audio.put(packet)
    sidecar = _FakeSidecar()

    sample_index, has_signal = session._send_direct_audio(
        sidecar,  # type: ignore[arg-type]
        _AudioPacer(),
        peer_epoch=1,
        sample_index=0,
        now=10.0,
        capture_ages_ms=deque(),
        capture_max_age_seconds=(
            session_module._DIRECT_STARTUP_CAPTURE_MAX_AGE_SECONDS
        ),
    )

    assert sample_index == 320
    assert has_signal
    assert sidecar.audio == [(packet.data, 0, 5_000_000_000)]


def test_input_activity_ignores_floor_but_keeps_long_speech_alive() -> None:
    assert not _pcm_has_signal((255).to_bytes(2, "little", signed=True) * 8)
    assert _pcm_has_signal((-256).to_bytes(2, "little", signed=True) * 8)


def test_pulseaudio_local_barge_in_signal_requires_peak_and_sustained_energy() -> None:
    assert not _pcm_has_local_barge_in_signal(b"\0" * 2_048)
    assert not _pcm_has_local_barge_in_signal(
        (1_023).to_bytes(2, "little", signed=True) * 1_024
    )
    assert not _pcm_has_local_barge_in_signal(
        (1_024).to_bytes(2, "little", signed=True) + b"\0" * 2_046
    )
    assert _pcm_has_local_barge_in_signal(
        (1_024).to_bytes(2, "little", signed=True) * 1_024
    )


def test_native_aec3_local_barge_in_uses_post_gain_speech_boundary() -> None:
    below_boundary = struct.pack(
        "<1024h",
        *([64, -64] * 75 + [0] * 874),
    )
    at_boundary = struct.pack(
        "<1024h",
        *([65, -65] * 80 + [0] * 864),
    )
    measured_user_speech = struct.pack(
        "<1024h",
        *([86, -86] * 64 + [0] * 896),
    )
    measured_playback_residual = struct.pack(
        "<1024h",
        *([90, -90] * 25 + [0] * 974),
    )

    assert session_module._pcm_peak_and_rms(below_boundary) == (64, 24)
    assert not _pcm_has_local_barge_in_signal(
        below_boundary,
        capture_backend=NATIVE_AEC3_CAPTURE,
        direct_capture_gain_db=12.0,
    )
    assert session_module._pcm_peak_and_rms(at_boundary) == (65, 25)
    assert _pcm_has_local_barge_in_signal(
        at_boundary,
        capture_backend=NATIVE_AEC3_CAPTURE,
        direct_capture_gain_db=12.0,
    )
    assert session_module._pcm_peak_and_rms(measured_user_speech) == (86, 30)
    assert _pcm_has_local_barge_in_signal(
        measured_user_speech,
        capture_backend=NATIVE_AEC3_CAPTURE,
        direct_capture_gain_db=12.0,
    )
    assert session_module._pcm_peak_and_rms(measured_playback_residual) == (90, 19)
    assert not _pcm_has_local_barge_in_signal(
        measured_playback_residual,
        capture_backend=NATIVE_AEC3_CAPTURE,
        direct_capture_gain_db=12.0,
    )


@pytest.mark.parametrize(
    ("capture_backend", "expected_guard_type"),
    [
        (PULSEAUDIO_AEC_CAPTURE, _RenderEchoGuard),
        (NATIVE_AEC3_CAPTURE, type(None)),
    ],
)
def test_render_echo_guard_is_used_only_for_pulseaudio_capture(
    capture_backend: str,
    expected_guard_type: type[object],
) -> None:
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            capture_backend=capture_backend,
        ),
        aec_verifier=lambda _config: None,
    )

    assert isinstance(session._render_echo_guard, expected_guard_type)


def test_render_echo_guard_calibrates_only_during_settle_and_rejects_echo() -> None:
    guard, features = _calibrated_render_echo_guard()
    capture, captured_at = _guard_capture(features, start=1_168)

    decision = guard.classify(
        capture,
        captured_at=captured_at,
        output_epoch=1,
        calibrating=False,
    )

    assert decision is not None
    assert decision.kind is _EchoDecisionKind.ECHO
    assert decision.correlation_permille >= 990
    assert 156 <= decision.delay_ms <= 164


def test_render_echo_guard_keeps_model_across_long_reusable_player_gap() -> None:
    guard, _features = _calibrated_render_echo_guard()
    trained_frames = guard._fir_valid_frames
    stable_delay = guard._stable_delay_samples
    guard.deactivate()
    guard.begin_epoch(2, reset=False)
    next_features = _render_features(1_000, seed=0xABCDEF01)
    written_at = 2.0
    guard.observe_render(
        _expanded_pcm(
            next_features,
            session_module._RENDER_ECHO_RENDER_DOWNSAMPLE,
        ),
        written_at=written_at,
    )
    start = 400
    frame = next_features[start : start + 256]
    captured_at = (
        written_at
        + session_module._RENDER_ECHO_NOMINAL_PLAYOUT_SECONDS
        + start / session_module._RENDER_ECHO_FEATURE_RATE
        + 0.160
        + len(frame) / session_module._RENDER_ECHO_FEATURE_RATE
    )

    decision = guard.classify(
        _expanded_pcm(
            frame,
            session_module._RENDER_ECHO_CAPTURE_DOWNSAMPLE,
            numerator=1,
            denominator=2,
            offset=700,
        ),
        captured_at=captured_at,
        output_epoch=2,
        calibrating=False,
    )

    assert guard._fir_valid_frames == trained_frames
    assert guard._stable_delay_samples == stable_delay
    assert decision is not None
    assert decision.kind is _EchoDecisionKind.ECHO
    assert decision.correlation_permille >= 990


def test_render_echo_guard_learns_polarity_inverted_echo() -> None:
    guard = _RenderEchoGuard()
    features = _render_features(3_200)
    guard.begin_epoch(1, reset=True)
    guard.observe_render(
        _expanded_pcm(
            features,
            session_module._RENDER_ECHO_RENDER_DOWNSAMPLE,
        ),
        written_at=0.0,
    )
    for start in (400, 656, 912):
        capture, captured_at = _guard_capture(
            features,
            start=start,
            numerator=-1,
        )
        decision = guard.classify(
            capture,
            captured_at=captured_at,
            output_epoch=1,
            calibrating=True,
        )
        assert decision is not None
        assert decision.kind is _EchoDecisionKind.ECHO

    capture, captured_at = _guard_capture(
        features,
        start=1_168,
        numerator=-1,
    )
    decision = guard.classify(
        capture,
        captured_at=captured_at,
        output_epoch=1,
        calibrating=False,
    )

    assert decision is not None
    assert decision.kind is _EchoDecisionKind.ECHO
    assert decision.correlation_permille >= 990


def test_odd_playback_writes_reassemble_into_continuous_render_reference() -> None:
    now = [0.0]
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
    )
    session._set_local_output_epoch(1, settle_barge_in=True)
    guard = session._render_echo_guard
    assert guard is not None
    features = _render_features(3_200)
    rendered = _expanded_pcm(
        features,
        session_module._RENDER_ECHO_RENDER_DOWNSAMPLE,
    )
    sizes = (1, 959, 7, 2_003, 13, 4_095)
    offset = 0
    chunk_index = 0
    while offset < len(rendered):
        size = min(sizes[chunk_index % len(sizes)], len(rendered) - offset)
        session._observe_direct_playback_write(rendered[offset : offset + size])
        offset += size
        chunk_index += 1

    assert session._direct_render_observation_tail == b""
    assert guard._render_sample_tail == []
    assert tuple(guard._render_samples) == tuple(features)

    for start in (400, 656, 912):
        capture, captured_at = _guard_capture(features, start=start)
        decision = guard.classify(
            capture,
            captured_at=captured_at,
            output_epoch=1,
            calibrating=True,
        )
        assert decision is not None
        assert decision.kind is _EchoDecisionKind.ECHO
    capture, captured_at = _guard_capture(features, start=1_168)
    decision = guard.classify(
        capture,
        captured_at=captured_at,
        output_epoch=1,
        calibrating=False,
    )

    assert decision is not None
    assert decision.kind is _EchoDecisionKind.ECHO


def test_render_echo_guard_tracks_cubic_retarget_without_barge_blackout() -> None:
    guard, features = _calibrated_render_echo_guard()
    retargeted = _render_features(800, seed=0x11223344)
    guard.observe_render(
        _expanded_pcm(
            retargeted,
            session_module._RENDER_ECHO_RENDER_DOWNSAMPLE,
            numerator=1,
            denominator=8,
        ),
        written_at=len(features) / session_module._RENDER_ECHO_FEATURE_RATE,
    )
    captured_at = (
        session_module._RENDER_ECHO_NOMINAL_PLAYOUT_SECONDS
        + len(features) / session_module._RENDER_ECHO_FEATURE_RATE
        + 0.160
        + 0.064
    )
    echo = _expanded_pcm(
        retargeted[:256],
        session_module._RENDER_ECHO_CAPTURE_DOWNSAMPLE,
        numerator=1,
        denominator=16,
        offset=700,
    )

    echo_decision = guard.classify(
        echo,
        captured_at=captured_at,
        output_epoch=1,
        calibrating=False,
    )

    assert echo_decision is not None
    assert echo_decision.kind is _EchoDecisionKind.ECHO

    near_end = _render_features(256, seed=0x55667788)
    mixed = [
        render_sample // 16 + user_sample + 700
        for render_sample, user_sample in zip(
            retargeted[:256],
            near_end,
            strict=True,
        )
    ]
    mixed_decision = guard.classify(
        _expanded_pcm(
            mixed,
            session_module._RENDER_ECHO_CAPTURE_DOWNSAMPLE,
        ),
        captured_at=captured_at,
        output_epoch=1,
        calibrating=False,
    )

    assert mixed_decision is not None
    assert mixed_decision.kind is _EchoDecisionKind.NEAR_END
    assert mixed_decision.interrupt_qualified


def test_render_echo_guard_fails_open_before_calibration() -> None:
    guard = _RenderEchoGuard()
    features = _render_features(1_000)
    guard.begin_epoch(1, reset=True)
    guard.observe_render(
        _expanded_pcm(
            features,
            session_module._RENDER_ECHO_RENDER_DOWNSAMPLE,
        ),
        written_at=0.0,
    )
    capture, captured_at = _guard_capture(features, start=400)

    decision = guard.classify(
        capture,
        captured_at=captured_at,
        output_epoch=1,
        calibrating=False,
    )

    assert decision is not None
    assert decision.kind is _EchoDecisionKind.NEAR_END
    assert not decision.interrupt_qualified


def test_render_echo_guard_preserves_genuine_double_talk() -> None:
    guard, features = _calibrated_render_echo_guard()
    start = 1_168
    echo = features[start : start + 256]
    near_end = _render_features(256, seed=0x2468ACE0)
    mixed = [
        render_sample // 2 + user_sample + 700
        for render_sample, user_sample in zip(echo, near_end, strict=True)
    ]
    capture = _expanded_pcm(
        mixed,
        session_module._RENDER_ECHO_CAPTURE_DOWNSAMPLE,
    )
    _echo_capture, captured_at = _guard_capture(features, start=start)

    decision = guard.classify(
        capture,
        captured_at=captured_at,
        output_epoch=1,
        calibrating=False,
    )

    assert decision is not None
    assert decision.kind is _EchoDecisionKind.NEAR_END
    assert decision.interrupt_qualified


def test_render_echo_guard_quiet_stale_and_missing_reference_fail_open() -> None:
    features = _render_features(256)
    capture = _expanded_pcm(
        features,
        session_module._RENDER_ECHO_CAPTURE_DOWNSAMPLE,
    )
    guard = _RenderEchoGuard()
    guard.begin_epoch(1, reset=True)

    missing = guard.classify(
        capture,
        captured_at=1.0,
        output_epoch=1,
        calibrating=False,
    )
    guard.observe_render(
        b"\0\0" * (512 * session_module._RENDER_ECHO_RENDER_DOWNSAMPLE),
        written_at=0.0,
    )
    quiet = guard.classify(
        capture,
        captured_at=0.4,
        output_epoch=1,
        calibrating=True,
    )
    stale = guard.classify(
        capture,
        captured_at=10.0,
        output_epoch=1,
        calibrating=True,
    )

    assert missing is not None and missing.kind is _EchoDecisionKind.NEAR_END
    assert quiet is not None and quiet.kind is _EchoDecisionKind.NEAR_END
    assert stale is not None and stale.kind is _EchoDecisionKind.NEAR_END
    assert not missing.interrupt_qualified
    assert not quiet.interrupt_qualified
    assert not stale.interrupt_qualified


def test_render_echo_guard_is_bounded_and_resets_partial_epoch_state() -> None:
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        aec_verifier=lambda _config: None,
    )
    guard = session._render_echo_guard
    assert guard is not None
    session._set_local_output_epoch(1, settle_barge_in=True)
    session._observe_direct_playback_write(b"\x01")
    assert session._direct_render_observation_tail == b"\x01"

    session._set_local_output_epoch(2)
    assert session._direct_render_observation_tail == b""
    session._observe_direct_playback_write(struct.pack("<4h", 1, 2, 3, 4))
    assert guard._render_sample_tail == [1, 2, 3, 4]
    session._set_local_output_epoch(3)
    assert guard._render_sample_tail == []

    guard.observe_render(
        _expanded_pcm(
            _render_features(session_module._RENDER_ECHO_RING_SAMPLES + 500),
            session_module._RENDER_ECHO_RENDER_DOWNSAMPLE,
        ),
        written_at=0.0,
    )
    assert len(guard._render_samples) == session_module._RENDER_ECHO_RING_SAMPLES

    player = _RecordingPlayer()
    session._abort_player(player)
    assert player.events == [("abort", None)]
    assert guard._epoch is None
    assert not guard._render_samples
    assert not guard._render_sample_tail


def test_direct_first_playback_settle_rejects_echo_before_arming_barge_in() -> None:
    now = [50.0]
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            input_queue_bytes=16_384,
        ),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    session._set_local_output_epoch(1, settle_barge_in=True)
    speech_like_echo = (2_000).to_bytes(2, "little", signed=True) * 1_024

    assert session.submit_audio(speech_like_echo) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech_like_echo) is SubmitResult.ACCEPTED
    assert session._local_barge_in_requested_epoch is None

    # A short receiver-quiet boundary must not restart or prematurely clear the
    # physical player's one-time AEC convergence window.
    session._set_local_output_epoch(None)
    now[0] += 0.2
    session._set_local_output_epoch(2)
    assert session.submit_audio(speech_like_echo) is SubmitResult.ACCEPTED
    assert session._local_barge_in_requested_epoch is None

    now[0] += 0.313
    assert session.submit_audio(speech_like_echo) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech_like_echo) is SubmitResult.ACCEPTED
    assert session._local_barge_in_requested_epoch == 2
    assert session._local_barge_in_requested_watermark == 5


def test_direct_playback_settle_preserves_parent_capture_for_the_sidecar() -> None:
    now = [50.0]
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            input_queue_bytes=8_192,
        ),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    session._set_local_output_epoch(1, settle_barge_in=True)
    speech_like_echo = (2_000).to_bytes(2, "little", signed=True) * 1_024

    assert session.submit_audio(speech_like_echo) is SubmitResult.ACCEPTED
    packet, remaining = session._audio.pop()
    assert packet is not None
    assert packet.data is speech_like_echo
    assert remaining == 0

    now[0] += 0.513
    assert session.submit_audio(speech_like_echo) is SubmitResult.ACCEPTED
    packet, remaining = session._audio.pop()
    assert packet is not None
    assert packet.data is speech_like_echo
    assert remaining == 0


def test_dynamic_volume_clamps_without_muting_capture_or_mutating_sink() -> None:
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            playback_volume_percent=60,
            aec_sink_volume_ceiling_percent=60,
            input_queue_bytes=8_192,
        ),
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    session._set_local_output_epoch(1)
    speech_like_echo = (2_000).to_bytes(2, "little", signed=True) * 1_024

    assert session.request_playback_volume(100) == 60
    assert session.request_playback_volume(30) == 30
    assert session.submit_audio(speech_like_echo) is SubmitResult.ACCEPTED
    packet, _remaining = session._audio.pop()
    assert packet is not None and packet.data is speech_like_echo
    assert session._local_barge_in_requested_epoch is None

    assert session.submit_audio(speech_like_echo) is SubmitResult.ACCEPTED
    assert session._local_barge_in_requested_epoch == 1
    assert session._local_barge_in_requested_watermark == 2


def test_render_echo_rejection_sends_equal_length_provider_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [10.0]
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            input_queue_bytes=8_192,
        ),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    session._direct_diagnostics = _DirectSessionDiagnostics(started_at=now[0])
    session._set_local_output_epoch(1)
    guard = session._render_echo_guard
    assert guard is not None
    monkeypatch.setattr(
        guard,
        "classify",
        lambda *_args, **_kwargs: _EchoDecision(
            _EchoDecisionKind.ECHO,
            1,
            correlation_permille=990,
            delay_ms=160,
        ),
    )
    first = struct.pack("<1024h", *([2_000] * 1_024))
    second = struct.pack("<1024h", *([-2_000] * 1_024))

    assert session.submit_audio(first) is SubmitResult.ACCEPTED
    assert session.submit_audio(second) is SubmitResult.ACCEPTED
    assert session._local_barge_in_requested_epoch is None

    sidecar = _FakeSidecar()
    pacer = _AudioPacer()
    sample_index, _signal = session._send_direct_audio(
        sidecar,  # type: ignore[arg-type]
        pacer,
        peer_epoch=1,
        sample_index=0,
        now=now[0],
        capture_ages_ms=deque(),
    )
    now[0] += 0.064
    session._send_direct_audio(
        sidecar,  # type: ignore[arg-type]
        pacer,
        peer_epoch=1,
        sample_index=sample_index,
        now=now[0],
        capture_ages_ms=deque(),
    )

    assert [packet[0] for packet in sidecar.audio] == [bytes(len(first))] * 2
    assert session._direct_diagnostics.echo_rejected_frames == 2
    assert session._direct_diagnostics.provider_suppressed_frames == 2
    assert session._direct_diagnostics.echo_max_correlation_permille == 990
    assert session._direct_diagnostics.echo_last_delay_ms == 160


def test_provider_suppression_tag_is_scoped_to_the_origin_peer_preroll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [10.0]
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            input_queue_bytes=8_192,
        ),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    session._set_local_output_epoch(1)
    guard = session._render_echo_guard
    assert guard is not None
    monkeypatch.setattr(
        guard,
        "classify",
        lambda *_args, **_kwargs: _EchoDecision(
            _EchoDecisionKind.ECHO,
            1,
            correlation_permille=995,
            delay_ms=160,
            reference_matched=True,
        ),
    )
    capture = struct.pack("<1024h", *([2_000] * 1_024))

    assert session.submit_audio(capture) is SubmitResult.ACCEPTED
    old_peer = _FakeSidecar()
    sample_index, _signal = session._send_direct_audio(
        old_peer,  # type: ignore[arg-type]
        _AudioPacer(),
        peer_epoch=1,
        sample_index=0,
        now=now[0],
        capture_ages_ms=deque(),
    )

    assert sample_index == len(capture) // 2
    assert old_peer.audio[0][0] == bytes(len(capture))
    assert session._sent_capture_watermark == 1
    assert [packet.data for packet in session._direct_preroll] == [capture]
    assert [packet.suppress_peer_epoch for packet in session._direct_preroll] == [1]

    session._begin_direct_rollover_capture(1)
    session._set_direct_peer_epoch(2)
    fresh_peer = _FakeSidecar()
    fresh_sample_index, _signal = session._send_direct_audio(
        fresh_peer,  # type: ignore[arg-type]
        _AudioPacer(),
        peer_epoch=2,
        sample_index=0,
        now=now[0],
        capture_ages_ms=deque(),
    )

    assert fresh_sample_index == len(capture) // 2
    assert fresh_peer.audio[0][0] is capture
    assert len(fresh_peer.audio[0][0]) == len(old_peer.audio[0][0])


def test_unsent_provider_suppressed_backlog_is_raw_for_a_fresh_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [20.0]
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            input_queue_bytes=8_192,
        ),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    session._set_local_output_epoch(1)
    guard = session._render_echo_guard
    assert guard is not None
    monkeypatch.setattr(
        guard,
        "classify",
        lambda *_args, **_kwargs: _EchoDecision(
            _EchoDecisionKind.ECHO,
            1,
            correlation_permille=995,
            delay_ms=160,
            reference_matched=True,
        ),
    )
    captures = (
        struct.pack("<1024h", *([2_000] * 1_024)),
        struct.pack("<1024h", *([-2_000] * 1_024)),
    )
    for capture in captures:
        assert session.submit_audio(capture) is SubmitResult.ACCEPTED

    session._set_direct_peer_epoch(2)
    fresh_peer = _FakeSidecar()
    pacer = _AudioPacer()
    sample_index = 0
    for _capture in captures:
        sample_index, _signal = session._send_direct_audio(
            fresh_peer,  # type: ignore[arg-type]
            pacer,
            peer_epoch=2,
            sample_index=sample_index,
            now=now[0],
            capture_ages_ms=deque(),
        )
        now[0] += 0.064

    assert [packet[0] for packet in fresh_peer.audio] == list(captures)
    assert all(
        sent is original
        for (sent, _sample_index, _captured_ns), original in zip(
            fresh_peer.audio,
            captures,
            strict=True,
        )
    )


def test_no_output_follow_up_capture_stays_byte_exact_for_provider() -> None:
    now = 30.0
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        clock=lambda: now,
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    session._set_direct_peer_epoch(2)
    follow_up = struct.pack("<1024h", *([1_500, -1_500] * 512))

    assert not session.output_active
    assert session.submit_audio(follow_up) is SubmitResult.ACCEPTED
    peer = _FakeSidecar()
    session._send_direct_audio(
        peer,  # type: ignore[arg-type]
        _AudioPacer(),
        peer_epoch=2,
        sample_index=0,
        now=now,
        capture_ages_ms=deque(),
    )

    assert peer.audio[0][0] is follow_up
    assert peer.audio[0][1:] == (0, int(now * 1_000_000_000))


def test_provider_visible_echo_below_local_barge_threshold_is_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 40.0
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            direct_capture_gain_db=6.0,
        ),
        clock=lambda: now,
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    session._set_local_output_epoch(1)
    guard = session._render_echo_guard
    assert guard is not None
    monkeypatch.setattr(
        guard,
        "classify",
        lambda *_args, **_kwargs: _EchoDecision(
            _EchoDecisionKind.ECHO,
            1,
            correlation_permille=990,
            delay_ms=160,
            reference_matched=True,
        ),
    )
    quiet_echo = struct.pack("<1024h", *([200, -200] * 512))

    assert not _pcm_has_local_barge_in_signal(quiet_echo)
    assert round(200 * (10 ** (6.0 / 20))) >= session_module._INPUT_ACTIVITY_SIGNAL_PEAK
    assert session.submit_audio(quiet_echo) is SubmitResult.ACCEPTED
    assert session._local_barge_in_requested_epoch is None
    peer = _FakeSidecar()
    session._send_direct_audio(
        peer,  # type: ignore[arg-type]
        _AudioPacer(),
        peer_epoch=1,
        sample_index=0,
        now=now,
        capture_ages_ms=deque(),
    )

    assert peer.audio[0][0] == bytes(len(quiet_echo))


def test_clear_low_correlation_near_end_passes_raw_to_current_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 50.0
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        clock=lambda: now,
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    session._set_local_output_epoch(1)
    guard = session._render_echo_guard
    assert guard is not None
    monkeypatch.setattr(
        guard,
        "classify",
        lambda *_args, **_kwargs: _EchoDecision(
            _EchoDecisionKind.NEAR_END,
            1,
            correlation_permille=300,
            delay_ms=160,
            reference_matched=True,
        ),
    )
    near_end = struct.pack("<1024h", *([2_000, -2_000] * 512))

    assert session.submit_audio(near_end) is SubmitResult.ACCEPTED
    peer = _FakeSidecar()
    session._send_direct_audio(
        peer,  # type: ignore[arg-type]
        _AudioPacer(),
        peer_epoch=1,
        sample_index=0,
        now=now,
        capture_ages_ms=deque(),
    )

    assert peer.audio[0][0] is near_end
    assert session._local_barge_in_frames == 1
    assert session._local_barge_in_requested_epoch is None


def test_high_correlation_near_end_is_old_peer_suppressed_but_replayed_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [60.0]
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            input_queue_bytes=8_192,
        ),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    session._set_local_output_epoch(1)
    guard = session._render_echo_guard
    assert guard is not None
    monkeypatch.setattr(
        guard,
        "classify",
        lambda *_args, **_kwargs: _EchoDecision(
            _EchoDecisionKind.NEAR_END,
            1,
            correlation_permille=900,
            delay_ms=160,
            reference_matched=True,
        ),
    )
    captures = (
        struct.pack("<1024h", *([2_000, -2_000] * 512)),
        struct.pack("<1024h", *([-2_000, 2_000] * 512)),
    )
    old_peer = _FakeSidecar()
    old_pacer = _AudioPacer()
    sample_index = 0
    for capture in captures:
        assert session.submit_audio(capture) is SubmitResult.ACCEPTED
        sample_index, _signal = session._send_direct_audio(
            old_peer,  # type: ignore[arg-type]
            old_pacer,
            peer_epoch=1,
            sample_index=sample_index,
            now=now[0],
            capture_ages_ms=deque(),
        )
        now[0] += 0.064

    assert [packet[0] for packet in old_peer.audio] == [
        bytes(len(capture)) for capture in captures
    ]
    assert session._local_barge_in_requested_epoch == 1
    assert session._local_barge_in_requested_watermark == 2

    player = _RecordingPlayer()
    player.begin(1)
    output_epoch, trigger_watermark = session._flush_local_barge_in(
        player,
        output_epoch=1,
        last_output_epoch=1,
    )
    assert output_epoch is None
    assert trigger_watermark == 2
    session._begin_direct_rollover_capture(trigger_watermark)
    session._set_direct_peer_epoch(2)

    fresh_peer = _FakeSidecar()
    fresh_pacer = _AudioPacer()
    sample_index = 0
    for _capture in captures:
        sample_index, _signal = session._send_direct_audio(
            fresh_peer,  # type: ignore[arg-type]
            fresh_pacer,
            peer_epoch=2,
            sample_index=sample_index,
            now=now[0],
            capture_ages_ms=deque(),
        )
        now[0] += 0.064

    assert [packet[0] for packet in fresh_peer.audio] == list(captures)


def test_anchor_transition_freezes_fir_and_routes_correlated_capture_safely() -> None:
    guard, features = _calibrated_render_echo_guard()
    capture, captured_at = _guard_capture(features, start=400)
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        clock=lambda: captured_at,
        aec_verifier=lambda _config: None,
    )
    session._render_echo_guard = guard
    session._set_local_output_epoch(1)
    assert guard.repair_boundary(1)
    guard.observe_render(
        _expanded_pcm(
            features,
            session_module._RENDER_ECHO_RENDER_DOWNSAMPLE,
        ),
        written_at=0.0,
    )
    session._local_anchor_requalification_pending = True
    with session._state_lock:
        session._state = SessionState.READY
    model_before = (
        tuple(guard._fir),
        guard._fir_valid_frames,
        tuple(guard._calibration_delays),
        guard._stable_delay_samples,
        guard._repair_active,
        guard._repair_qualified,
    )

    session._arm_local_anchor_repair_transition(time.monotonic() + 1.0)
    try:
        assert session.submit_audio(capture) is SubmitResult.ACCEPTED
        assert session._local_barge_in_requested_epoch is None
        peer = _FakeSidecar()
        session._send_direct_audio(
            peer,  # type: ignore[arg-type]
            _AudioPacer(),
            peer_epoch=1,
            sample_index=0,
            now=captured_at,
            capture_ages_ms=deque(),
        )
    finally:
        session._finish_local_anchor_repair_transition()

    model_after = (
        tuple(guard._fir),
        guard._fir_valid_frames,
        tuple(guard._calibration_delays),
        guard._stable_delay_samples,
        guard._repair_active,
        guard._repair_qualified,
    )
    assert model_after == model_before
    assert peer.audio[0][0] == bytes(len(capture))


def test_transition_fence_blocks_a_poised_classifier_model_commit() -> None:
    features = _render_features(1_000)
    guard = _RenderEchoGuard()
    guard.begin_epoch(1, reset=True)
    assert guard.repair_boundary(1) is False
    guard.observe_render(
        _expanded_pcm(
            features,
            session_module._RENDER_ECHO_RENDER_DOWNSAMPLE,
        ),
        written_at=0.0,
    )
    capture, captured_at = _guard_capture(features, start=400)
    commit_waiting = threading.Event()
    allow_commit = threading.Event()
    classifier_ident: list[int] = []

    class CommitGate:
        def __init__(self) -> None:
            self._real = threading.Lock()
            self._classifier_acquires = 0

        def acquire(
            self,
            blocking: bool = True,
            timeout: float = -1,
        ) -> bool:
            if classifier_ident and threading.get_ident() == classifier_ident[0]:
                self._classifier_acquires += 1
                if self._classifier_acquires == 2:
                    commit_waiting.set()
                    assert allow_commit.wait(1.0)
            if timeout == -1:
                return self._real.acquire(blocking)
            return self._real.acquire(blocking, timeout)

        def release(self) -> None:
            self._real.release()

        def __enter__(self) -> CommitGate:
            assert self.acquire()
            return self

        def __exit__(self, *_args: object) -> None:
            self.release()

    guard._lock = CommitGate()  # type: ignore[assignment]
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        aec_verifier=lambda _config: None,
    )
    session._render_echo_guard = guard
    decisions: list[_EchoDecision] = []

    def classify() -> None:
        classifier_ident.append(threading.get_ident())
        decision = guard.classify(
            capture,
            captured_at=captured_at,
            output_epoch=1,
            calibrating=True,
        )
        assert decision is not None
        decisions.append(decision)

    classifier = threading.Thread(target=classify, daemon=True)
    classifier.start()
    assert commit_waiting.wait(1.0)
    proof_before = (
        tuple(guard._fir),
        guard._fir_valid_frames,
        tuple(guard._calibration_delays),
        guard._stable_delay_samples,
    )

    session._arm_local_anchor_repair_transition(time.monotonic() + 0.5)
    try:
        # The fence returned while the already-classified frame was poised at
        # its commit lock. Releasing it now must not mutate either FIR or proof.
        allow_commit.set()
        classifier.join(1.0)
        assert not classifier.is_alive()
        assert len(decisions) == 1
        assert decisions[0].kind is _EchoDecisionKind.ECHO
        assert (
            tuple(guard._fir),
            guard._fir_valid_frames,
            tuple(guard._calibration_delays),
            guard._stable_delay_samples,
        ) == proof_before
    finally:
        allow_commit.set()
        session._finish_local_anchor_repair_transition()


def test_unseeded_repair_retains_residual_discrimination_after_bootstrap() -> None:
    features = _render_features(3_200)
    guard = _RenderEchoGuard()
    guard.begin_epoch(1, reset=True)
    assert guard.repair_boundary(1) is False
    guard.observe_render(
        _expanded_pcm(
            features,
            session_module._RENDER_ECHO_RENDER_DOWNSAMPLE,
        ),
        written_at=0.0,
    )
    now = [0.0]
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            input_queue_bytes=16_384,
        ),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
    )
    session._render_echo_guard = guard
    session._set_local_output_epoch(1)
    session._local_anchor_requalification_pending = True
    with session._state_lock:
        session._state = SessionState.READY
    decisions: list[_EchoDecisionKind] = []
    original_classify = guard.classify

    def classify_and_record(*args: object, **kwargs: object) -> _EchoDecision | None:
        decision = original_classify(*args, **kwargs)  # type: ignore[arg-type]
        assert decision is not None
        decisions.append(decision.kind)
        return decision

    guard.classify = classify_and_record  # type: ignore[method-assign]

    for start in (400, 656):
        echo, captured_at = _guard_capture(features, start=start)
        now[0] = captured_at
        assert session.submit_audio(echo) is SubmitResult.ACCEPTED

    assert decisions == [_EchoDecisionKind.ECHO, _EchoDecisionKind.ECHO]
    assert guard._fir_valid_frames == 2
    assert guard._repair_seeded

    near_end_features = _render_features(512, seed=0x2468ACE0)
    for offset, start in enumerate((912, 1_168)):
        echo = features[start : start + 256]
        user = near_end_features[offset * 256 : (offset + 1) * 256]
        double_talk = [
            echo_sample // 2 + user_sample + 700
            for echo_sample, user_sample in zip(echo, user, strict=True)
        ]
        _unused_echo, captured_at = _guard_capture(features, start=start)
        now[0] = captured_at
        assert (
            session.submit_audio(
                _expanded_pcm(
                    double_talk,
                    session_module._RENDER_ECHO_CAPTURE_DOWNSAMPLE,
                )
            )
            is SubmitResult.ACCEPTED
        )

    assert decisions == [
        _EchoDecisionKind.ECHO,
        _EchoDecisionKind.ECHO,
        _EchoDecisionKind.NEAR_END,
        _EchoDecisionKind.NEAR_END,
    ]
    assert session._local_barge_in_requested_epoch == 1
    assert session._local_barge_in_requested_watermark == 4
    packets = session._audio.drain()
    assert len(packets) == 4
    assert all(packet.suppress_peer_epoch == 1 for packet in packets)


def test_provider_suppression_annotation_linearizes_before_direct_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 70.0
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        clock=lambda: now,
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    session._set_local_output_epoch(1)
    guard = session._render_echo_guard
    assert guard is not None
    monkeypatch.setattr(
        guard,
        "classify",
        lambda *_args, **_kwargs: _EchoDecision(
            _EchoDecisionKind.ECHO,
            1,
            correlation_permille=990,
            delay_ms=160,
            reference_matched=True,
        ),
    )
    capture = struct.pack("<1024h", *([2_000] * 1_024))
    replacement_entered = threading.Event()
    release_replacement = threading.Event()
    original_replace_tail = session._audio.replace_tail

    def blocking_replace_tail(
        expected: _AudioPacket,
        replacement: _AudioPacket,
    ) -> bool:
        assert replacement.data is expected.data
        assert replacement.suppress_peer_epoch == 1
        replacement_entered.set()
        assert release_replacement.wait(1.0)
        return original_replace_tail(expected, replacement)

    monkeypatch.setattr(session._audio, "replace_tail", blocking_replace_tail)
    submit_results: list[SubmitResult] = []
    submit_thread = threading.Thread(
        target=lambda: submit_results.append(session.submit_audio(capture)),
        daemon=True,
    )
    submit_thread.start()
    assert replacement_entered.wait(1.0)

    peer = _FakeSidecar()
    send_thread = threading.Thread(
        target=lambda: session._send_direct_audio(
            peer,  # type: ignore[arg-type]
            _AudioPacer(),
            peer_epoch=1,
            sample_index=0,
            now=now,
            capture_ages_ms=deque(),
        ),
        daemon=True,
    )
    send_thread.start()
    try:
        assert not peer.audio
    finally:
        release_replacement.set()
    submit_thread.join(1.0)
    send_thread.join(1.0)

    assert not submit_thread.is_alive()
    assert not send_thread.is_alive()
    assert submit_results == [SubmitResult.ACCEPTED]
    assert peer.audio[0][0] == bytes(len(capture))
    assert session._audio.bytes == 0


def test_ambiguous_render_evidence_requires_four_capture_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            input_queue_bytes=8_192,
        ),
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    session._set_local_output_epoch(1)
    guard = session._render_echo_guard
    assert guard is not None
    monkeypatch.setattr(
        guard,
        "classify",
        lambda *_args, **_kwargs: _EchoDecision(
            _EchoDecisionKind.AMBIGUOUS,
            1,
            correlation_permille=500,
            delay_ms=160,
        ),
    )
    speech = (2_000).to_bytes(2, "little", signed=True) * 1_024

    for _ in range(3):
        assert session.submit_audio(speech) is SubmitResult.ACCEPTED
        assert session._local_barge_in_requested_epoch is None
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED

    assert session._local_barge_in_requested_epoch == 1
    assert session._local_barge_in_requested_watermark == 4


def test_alternating_render_evidence_fails_open_within_four_capture_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            input_queue_bytes=8_192,
        ),
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    session._set_local_output_epoch(1)
    guard = session._render_echo_guard
    assert guard is not None
    kinds = iter(
        (
            _EchoDecisionKind.AMBIGUOUS,
            _EchoDecisionKind.NEAR_END,
            _EchoDecisionKind.AMBIGUOUS,
            _EchoDecisionKind.NEAR_END,
        )
    )
    monkeypatch.setattr(
        guard,
        "classify",
        lambda *_args, **_kwargs: _EchoDecision(
            next(kinds),
            1,
            correlation_permille=500,
            delay_ms=160,
        ),
    )
    speech = (2_000).to_bytes(2, "little", signed=True) * 1_024

    for _ in range(3):
        assert session.submit_audio(speech) is SubmitResult.ACCEPTED
        assert session._local_barge_in_requested_epoch is None
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED

    assert session._local_barge_in_requested_epoch == 1
    assert session._local_barge_in_requested_watermark == 4


def test_device_webrtc_capture_queues_and_detects_original_pcm_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_at = 123.5
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        clock=lambda: captured_at,
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    detector_values: list[bytes] = []

    original_metrics = session_module._pcm_peak_and_rms

    def record_detector(value: bytes) -> tuple[int, int]:
        detector_values.append(value)
        return original_metrics(value)

    monkeypatch.setattr(
        session_module,
        "_pcm_peak_and_rms",
        record_detector,
    )
    # This frame would have been amplified by the removed amplitude-only
    # normalizer, despite measured self-echo being stronger than ambient input.
    frame = (100).to_bytes(2, "little", signed=True) * 1_024

    assert session.submit_audio(frame) is SubmitResult.ACCEPTED
    packet, remaining = session._audio.pop()

    assert packet is not None
    assert remaining == 0
    assert packet.data is frame
    assert detector_values == [frame]
    assert packet.captured_at == captured_at
    assert packet.capture_watermark == 1


def test_input_queue_is_nonblocking_bounded_and_pcm_aligned() -> None:
    session = RealtimeSession(_config(input_queue_bytes=4_096))
    with session._state_lock:
        session._state = SessionState.CONNECTING

    assert session.submit_audio(b"a" * 2_048) is SubmitResult.ACCEPTED
    assert session.submit_audio(b"b" * 2_048) is SubmitResult.ACCEPTED
    assert session.submit_audio(b"c" * 2) is SubmitResult.FULL
    assert session.submit_audio(b"odd") is SubmitResult.INVALID


def test_direct_full_queue_speech_trigger_fails_closed_without_fake_watermark() -> None:
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            input_queue_bytes=2_048,
        ),
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    session._set_local_output_epoch(1)
    speech = (2_000).to_bytes(2, "little", signed=True) * 1_024

    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.FULL

    assert session.state is SessionState.STOPPING
    assert session._direct_output_fenced.is_set()
    assert session._interrupt_requested.is_set()
    assert session._interrupt_preserve_session is False
    assert session._audio.bytes == 0
    assert session._local_barge_in_requested_epoch is None
    assert session._local_barge_in_requested_watermark is None


def test_direct_full_then_accepted_speech_uses_accepted_causal_watermark() -> None:
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            input_queue_bytes=2_048,
        ),
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    session._set_local_output_epoch(1)
    speech = (2_000).to_bytes(2, "little", signed=True) * 1_024
    quiet = b"\0" * 2_048

    assert session.submit_audio(quiet) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.FULL
    packet, _remaining = session._audio.pop()
    assert packet is not None and packet.capture_watermark == 1
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED

    player = _RecordingPlayer()
    player.begin(1)
    assert session._flush_local_barge_in(
        player,
        output_epoch=1,
        last_output_epoch=1,
    ) == (None, 2)
    assert not session._interrupt_requested.is_set()
    assert session.state is SessionState.READY


def test_direct_full_quiet_frame_resets_overflow_barge_counter() -> None:
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            input_queue_bytes=2_048,
        ),
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    session._set_local_output_epoch(1)
    speech = (2_000).to_bytes(2, "little", signed=True) * 1_024
    quiet = b"\0" * 2_048

    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(quiet) is SubmitResult.FULL
    packet, _remaining = session._audio.pop()
    assert packet is not None
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED

    assert session._local_barge_in_requested_epoch is None
    assert session._local_barge_in_requested_watermark is None
    assert not session._interrupt_requested.is_set()
    assert session.state is SessionState.READY


def test_message_bound_accepts_exactly_one_fixed_recorder_frame() -> None:
    session = RealtimeSession(_config(max_message_bytes=2_048))
    with session._state_lock:
        session._state = SessionState.CONNECTING

    assert session.submit_audio(b"a" * 2_048) is SubmitResult.ACCEPTED
    assert session.submit_audio(b"b" * 2_050) is SubmitResult.INVALID


def test_output_gates_half_duplex_but_verified_full_duplex_keeps_mic_open() -> None:
    session = RealtimeSession(_config())
    with session._state_lock:
        session._state = SessionState.READY
    session._output_active.set()
    assert session.submit_audio(b"\0\0") is SubmitResult.GATED

    duplex = RealtimeSession(_duplex_config(), aec_verifier=lambda _config: None)
    with duplex._state_lock:
        duplex._state = SessionState.READY
    duplex._output_active.set()
    assert duplex.submit_audio(b"\0\0") is SubmitResult.ACCEPTED


def test_paplay_uses_fixed_low_latency_argv_and_reaps_owned_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    calls: list[tuple[list[str], dict[str, object]]] = []
    writes: list[bytes] = []

    def popen(argv: list[str], **kwargs: object) -> Any:
        calls.append((argv, kwargs))
        return process

    monkeypatch.setattr("os.set_blocking", lambda _fd, _blocking: None)
    monkeypatch.setattr(
        "os.write",
        lambda _fd, value: writes.append(bytes(value)) or len(value),
    )
    player = _PcmPlayer(4_096, popen=popen)

    player.begin(7)
    player.enqueue(b"\x01\x00" * 64)
    player.finish(7)

    assert calls == [
        (
            list(_PAPLAY_ARGV),
            {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "close_fds": True,
                "start_new_session": True,
                "shell": False,
            },
        )
    ]
    assert "--latency-msec=60" in calls[0][0]
    assert "--process-time-msec=20" in calls[0][0]
    assert writes == [b"\x01\x00" * 64]
    assert process.stdin.closed
    assert player.active

    process.returncode = 0
    player.service()
    assert player.active is False
    assert process.waited == 1


def test_software_volume_is_bounded_and_ramps_without_clipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "_PLAYBACK_VOLUME_RAMP_SAMPLES", 4)
    attenuator = _PlaybackAttenuator(60)
    source = struct.pack("<6h", *([1_000] * 6))

    assert attenuator.request(0, ramp=True) == 0
    assert struct.unpack("<6h", attenuator.scale(source)) == (
        750,
        500,
        250,
        0,
        0,
        0,
    )
    assert attenuator.request(30, ramp=False) == 30
    assert struct.unpack("<6h", attenuator.scale(source)) == (125,) * 6
    assert attenuator.request(100, ramp=False) == 60
    assert attenuator.scale(source) is source
    assert attenuator.request(-10, ramp=False) == 0
    assert attenuator.scale(source) == bytes(len(source))

    with pytest.raises(ValueError, match="integer"):
        attenuator.request(True, ramp=False)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="aligned"):
        attenuator.scale(b"odd")


def test_duplicate_volume_request_does_not_restart_an_in_progress_ramp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "_PLAYBACK_VOLUME_RAMP_SAMPLES", 4)
    attenuator = _PlaybackAttenuator(60)
    two_samples = struct.pack("<2h", 1_000, 1_000)

    assert attenuator.request(0, ramp=True) == 0
    assert struct.unpack("<2h", attenuator.scale(two_samples)) == (750, 500)
    assert attenuator.request(0, ramp=True) == 0
    assert struct.unpack("<2h", attenuator.scale(two_samples)) == (250, 0)

    assert attenuator.request(60, ramp=True) == 60
    assert struct.unpack("<h", attenuator.scale(struct.pack("<h", 1_000))) == (250,)
    assert attenuator.request(30, ramp=True) == 30
    # Retargeting starts from the already rendered gain, with no discontinuity.
    first_retargeted = struct.unpack("<h", attenuator.scale(struct.pack("<h", 1_000)))[
        0
    ]
    assert 125 < first_retargeted < 250


def test_volume_requested_before_start_is_used_by_the_default_direct_player() -> None:
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            playback_volume_percent=60,
            aec_sink_volume_ceiling_percent=60,
        ),
        aec_verifier=lambda _config: None,
    )
    source = struct.pack("<2h", 1_000, -1_000)

    assert session.request_playback_volume(30) == 30
    player = session._direct_player_factory(4_096, DEFAULT_PULSE_AEC_SINK)

    assert isinstance(player, _PcmPlayer)
    assert player._pcm_transform is not None
    assert player._pcm_transform(source) == struct.pack("<2h", 125, -125)


@pytest.mark.parametrize(
    "state",
    [SessionState.CONNECTING, SessionState.READY, SessionState.INTERRUPTING],
)
def test_dynamic_volume_accepts_live_session_states(state: SessionState) -> None:
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = state

    assert session.request_playback_volume(20) == 20


@pytest.mark.parametrize(
    "state",
    [SessionState.STOPPING, SessionState.STOPPED, SessionState.FAILED],
)
def test_dynamic_volume_rejects_terminal_session_states(state: SessionState) -> None:
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = state

    with pytest.raises(RuntimeError, match="no longer accepts"):
        session.request_playback_volume(30)

    source = struct.pack("<h", 1_000)
    assert session._playback_attenuator.scale(source) == source


@pytest.mark.parametrize(
    "state",
    [SessionState.CONNECTING, SessionState.READY, SessionState.INTERRUPTING],
)
def test_volume_reconciliation_checks_exact_anchor_before_software_gain(
    state: SessionState,
) -> None:
    reconciled: list[RealtimeConfig] = []
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            playback_volume_percent=60,
            aec_sink_volume_ceiling_percent=60,
        ),
        aec_verifier=lambda _config: None,
        anchor_reconciler=lambda config: reconciled.append(config) is None and False,
    )
    with session._state_lock:
        session._state = state

    assert session.reconcile_playback_volume(30) == 30

    assert reconciled == [session._config]
    assert session._playback_volume_percent == 30
    assert session.state is state
    assert not session._interrupt_requested.is_set()


@pytest.mark.parametrize("failure", ["exception", "invalid-result"])
def test_volume_reconciliation_failure_fences_session_and_clears_capture(
    failure: str,
) -> None:
    def reconcile(_config: RealtimeConfig) -> bool:
        if failure == "exception":
            raise WebSocketError("anchor probe failed")
        return 1  # type: ignore[return-value]

    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            playback_volume_percent=60,
            aec_sink_volume_ceiling_percent=60,
        ),
        aec_verifier=lambda _config: None,
        anchor_reconciler=reconcile,
    )
    with session._state_lock:
        session._state = SessionState.READY
    microphone_pcm = b"\x01\x00" * 32
    assert session.submit_audio(microphone_pcm) is SubmitResult.ACCEPTED

    with pytest.raises(WebSocketError):
        session.reconcile_playback_volume(30)

    assert session.state is SessionState.STOPPING
    assert session._interrupt_requested.is_set()
    assert session._interrupt_preserve_session is False
    assert session._audio.pop() == (None, 0)
    assert session.submit_audio(microphone_pcm) is SubmitResult.CLOSED
    assert session._playback_volume_percent == 60


def test_volume_reconciliation_arms_before_timed_output_lock_wait() -> None:
    observed_timeouts: list[float] = []
    reconciled: list[RealtimeConfig] = []
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        aec_verifier=lambda _config: None,
        anchor_reconciler=lambda config: reconciled.append(config) is None,
    )

    class RefusingOutputLock:
        def acquire(self, *, timeout: float) -> bool:
            assert session._local_anchor_transition.is_set()
            observed_timeouts.append(timeout)
            return False

        def release(self) -> None:
            raise AssertionError("an unacquired output lock was released")

    session._direct_output_lock = RefusingOutputLock()  # type: ignore[assignment]
    with session._state_lock:
        session._state = SessionState.READY

    with pytest.raises(WebSocketError, match="lock timed out"):
        session.reconcile_playback_volume(30)

    assert len(observed_timeouts) == 1
    assert 0 < observed_timeouts[0] <= 0.075
    assert reconciled == []
    assert session._direct_output_fenced.is_set()
    assert session._interrupt_requested.is_set()
    assert session.state is SessionState.STOPPING
    assert not session._local_anchor_transition.is_set()


def test_native_aec3_anchor_repair_does_not_arm_render_requalification() -> None:
    now = [10.0]
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            capture_backend=NATIVE_AEC3_CAPTURE,
            playback_volume_percent=60,
            aec_sink_volume_ceiling_percent=60,
        ),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
        anchor_reconciler=lambda _config: True,
    )
    session._set_local_output_epoch(1)
    with session._state_lock:
        session._state = SessionState.READY

    assert session.reconcile_playback_volume(30) == 30

    assert session._render_echo_guard is None
    assert not session._local_anchor_requalification_pending
    assert session._local_anchor_requalification_evidence_frames == 0
    assert not session._local_anchor_requalification_failed.is_set()
    assert not session._direct_output_fenced.is_set()
    assert not session._interrupt_requested.is_set()
    assert session.state is SessionState.READY


def test_native_aec3_playback_residual_does_not_fence_after_anchor_repair() -> None:
    now = [10.0]
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            capture_backend=NATIVE_AEC3_CAPTURE,
            direct_capture_gain_db=12.0,
            playback_volume_percent=60,
            aec_sink_volume_ceiling_percent=60,
        ),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
        anchor_reconciler=lambda _config: True,
    )
    session._set_local_output_epoch(1)
    with session._state_lock:
        session._state = SessionState.READY
    assert session.reconcile_playback_volume(30) == 30

    # The playback-only native-AEC canary peaked at 90 / RMS 19. Its peak is
    # provider-visible after +12 dB, while sustained energy remains below the
    # local speech boundary.
    residual = struct.pack("<1024h", *([90, -90] * 25 + [0] * 974))
    assert session_module._pcm_peak_and_rms(residual) == (90, 19)
    assert not _pcm_has_local_barge_in_signal(
        residual,
        capture_backend=NATIVE_AEC3_CAPTURE,
        direct_capture_gain_db=12.0,
    )

    suppressions: list[int | None] = []
    for _ in range(
        session_module._LOCAL_BARGE_IN_ANCHOR_REPAIR_MAX_EVIDENCE_FRAMES + 1
    ):
        now[0] += 0.064
        assert session.submit_audio(residual) is SubmitResult.ACCEPTED
        packet, remaining = session._audio.pop()
        assert packet is not None and packet.data == residual
        suppressions.append(packet.suppress_peer_epoch)
        assert remaining == 0
        assert session.state is SessionState.READY

    assert suppressions == [1] + [None] * (
        session_module._LOCAL_BARGE_IN_ANCHOR_REPAIR_MAX_EVIDENCE_FRAMES
    )
    assert not session._local_anchor_requalification_pending
    assert session._local_anchor_requalification_evidence_frames == 0
    assert not session._local_anchor_requalification_failed.is_set()
    assert not session._direct_output_fenced.is_set()
    assert not session._interrupt_requested.is_set()
    assert session._local_barge_in_requested_epoch is None
    assert session._local_barge_in_requested_watermark is None


def test_native_aec3_measured_user_speech_qualifies_barge_in_at_twelve_db() -> None:
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            capture_backend=NATIVE_AEC3_CAPTURE,
            direct_capture_gain_db=12.0,
        ),
        clock=lambda: 10.0,
        aec_verifier=lambda _config: None,
    )
    session._set_local_output_epoch(1)
    with session._state_lock:
        session._state = SessionState.READY

    # This is the 86 / RMS 30 command observed before assistant playback in
    # the successful device trace; +12 dB made the same interval 342 / 132 at
    # the outbound WebRTC stage.
    speech = struct.pack("<1024h", *([86, -86] * 64 + [0] * 896))
    assert session_module._pcm_peak_and_rms(speech) == (86, 30)

    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session._local_barge_in_requested_epoch is None
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED

    assert session._local_barge_in_requested_epoch == 1
    assert session._local_barge_in_requested_watermark == 2
    assert not session._interrupt_requested.is_set()


def test_trained_active_anchor_repair_preserves_model_capture_and_short_settle() -> (
    None
):
    now = [10.0]
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            playback_volume_percent=60,
            aec_sink_volume_ceiling_percent=60,
        ),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
        anchor_reconciler=lambda _config: True,
    )
    guard, _features = _calibrated_render_echo_guard()
    session._render_echo_guard = guard
    session._set_local_output_epoch(1)
    with session._state_lock:
        session._state = SessionState.READY
    microphone_pcm = b"\x01\x00" * 32
    assert session.submit_audio(microphone_pcm) is SubmitResult.ACCEPTED
    session._direct_render_observation_tail = b"\x02\x00"
    session._local_barge_in_frames = 1
    session._local_barge_in_ambiguous_frames = 2
    fir_before = tuple(guard._fir)
    valid_frames_before = guard._fir_valid_frames
    stable_delay_before = guard._stable_delay_samples
    assert guard._render_samples

    assert session.reconcile_playback_volume(25) == 25

    packet, remaining = session._audio.pop()
    assert packet is not None
    assert packet.data == microphone_pcm
    assert packet.captured_at == 10.0
    assert remaining == 0
    assert session.state is SessionState.READY
    assert session._local_output_epoch == 1
    assert session.output_active
    assert tuple(guard._fir) == fir_before
    assert valid_frames_before >= session_module._RENDER_ECHO_CALIBRATION_FRAMES
    assert stable_delay_before is not None
    assert guard._fir_valid_frames == 0
    assert guard._stable_delay_samples is None
    assert session._local_anchor_requalification_pending
    assert guard.repair_status(1) == (True, False)
    assert not guard._render_samples
    assert guard._render_start_time is None
    assert guard._render_end_time is None
    assert session._direct_render_observation_tail == b""
    assert session._local_barge_in_frames == 0
    assert session._local_barge_in_ambiguous_frames == 0
    assert session._local_barge_in_settle_until == pytest.approx(10.128)
    assert session._local_barge_in_settle_until < (
        10.0 + session_module._LOCAL_BARGE_IN_PLAYBACK_SETTLE_SECONDS
    )


def test_anchor_repair_requalifies_after_three_same_generation_echo_frames() -> None:
    now = [0.0]
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            playback_volume_percent=60,
            aec_sink_volume_ceiling_percent=60,
        ),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
        anchor_reconciler=lambda _config: True,
    )
    guard, features = _calibrated_render_echo_guard()
    session._render_echo_guard = guard
    session._set_local_output_epoch(1)
    with session._state_lock:
        session._state = SessionState.READY

    assert session.reconcile_playback_volume(30) == 30
    assert session._local_anchor_requalification_pending
    assert guard.repair_status(1) == (True, False)
    guard.observe_render(
        _expanded_pcm(
            features,
            session_module._RENDER_ECHO_RENDER_DOWNSAMPLE,
        ),
        written_at=0.0,
    )

    for frame_number, start in enumerate((400, 656, 912), start=1):
        capture, captured_at = _guard_capture(features, start=start)
        now[0] = captured_at
        assert session.submit_audio(capture) is SubmitResult.ACCEPTED
        packet, remaining = session._audio.pop()
        assert packet is not None
        assert packet.data == capture
        assert remaining == 0
        expected_qualified = frame_number == 3
        assert guard.repair_status(1) == (True, expected_qualified)
        assert session._local_anchor_requalification_pending is (not expected_qualified)

    assert guard._fir_valid_frames == session_module._RENDER_ECHO_CALIBRATION_FRAMES
    assert guard._stable_delay_samples is not None
    assert session.state is SessionState.READY


def test_anchor_requalification_survives_quiet_zero_volume_and_media_quiet() -> None:
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            playback_volume_percent=60,
            aec_sink_volume_ceiling_percent=60,
        ),
        clock=lambda: 10.0,
        aec_verifier=lambda _config: None,
        anchor_reconciler=lambda _config: True,
    )
    guard, _features = _calibrated_render_echo_guard()
    session._render_echo_guard = guard
    session._set_local_output_epoch(1)
    with session._state_lock:
        session._state = SessionState.READY

    assert session.reconcile_playback_volume(0) == 0
    assert session.submit_audio(bytes(2_048)) is SubmitResult.ACCEPTED
    packet, remaining = session._audio.pop()
    assert packet is not None and packet.data == bytes(2_048)
    assert remaining == 0
    assert session._local_anchor_requalification_pending
    assert session._local_anchor_requalification_evidence_frames == 0

    state = session_module._DirectPlaybackState(
        active_generation=1,
        newest_generation=1,
    )
    assert session._handle_direct_lifecycle(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.quiet", "generation": 1},
        ),
        _FakeSidecar(),
        _RecordingPlayer(),
        state,
    )

    assert session._local_output_epoch is None
    assert not session.output_active
    assert session._local_anchor_requalification_pending
    assert session._local_anchor_requalification_evidence_frames == 0
    assert not session._local_anchor_requalification_failed.is_set()


def test_clear_near_end_remains_interruptible_after_repair_transition() -> None:
    now = [10.0]
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            playback_volume_percent=60,
            aec_sink_volume_ceiling_percent=60,
        ),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
        anchor_reconciler=lambda _config: True,
    )
    guard, _features = _calibrated_render_echo_guard()
    session._render_echo_guard = guard
    session._set_local_output_epoch(1)
    with session._state_lock:
        session._state = SessionState.READY
    assert session.reconcile_playback_volume(30) == 30
    guard.classify = lambda *_args, **_kwargs: _EchoDecision(  # type: ignore[method-assign]
        _EchoDecisionKind.NEAR_END,
        1,
    )
    guard.repair_status = lambda _epoch: (True, False)  # type: ignore[method-assign]
    now[0] += session_module._LOCAL_BARGE_IN_ANCHOR_REPAIR_SETTLE_SECONDS + 0.001
    near_end = (2_000).to_bytes(2, "little", signed=True) * 1_024

    assert session.submit_audio(near_end) is SubmitResult.ACCEPTED
    assert session.submit_audio(near_end) is SubmitResult.ACCEPTED

    assert session._local_anchor_requalification_pending
    assert session._local_anchor_requalification_evidence_frames == 0
    assert session._local_barge_in_requested_epoch == 1
    assert session._local_barge_in_requested_watermark == 2
    assert session.state is SessionState.READY


def test_untrained_active_anchor_drift_eventually_fences_output_closed() -> None:
    now = [10.0]
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            playback_volume_percent=60,
            aec_sink_volume_ceiling_percent=60,
        ),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
        anchor_reconciler=lambda _config: True,
    )
    session._set_local_output_epoch(1)
    with session._state_lock:
        session._state = SessionState.READY

    assert session.reconcile_playback_volume(30) == 30
    assert session.state is SessionState.READY
    assert session._local_anchor_requalification_pending
    assert session._render_echo_guard is not None
    guard = session._render_echo_guard
    guard.classify = lambda *_args, **_kwargs: _EchoDecision(  # type: ignore[method-assign]
        _EchoDecisionKind.AMBIGUOUS,
        1,
    )
    guard.repair_status = lambda _epoch: (True, False)  # type: ignore[method-assign]
    signal = (2_000).to_bytes(2, "little", signed=True) * 1_024

    for _ in range(
        session_module._LOCAL_BARGE_IN_ANCHOR_REPAIR_MAX_EVIDENCE_FRAMES - 1
    ):
        now[0] += 0.064
        assert session.submit_audio(signal) is SubmitResult.ACCEPTED
        assert session.state is SessionState.READY
        packet, remaining = session._audio.pop()
        assert packet is not None and packet.data == signal
        assert remaining == 0

    now[0] += 0.064
    assert session.submit_audio(signal) is SubmitResult.ACCEPTED

    assert session.state is SessionState.STOPPING
    assert session._direct_output_fenced.is_set()
    assert session._interrupt_requested.is_set()
    assert session._interrupt_preserve_session is False
    assert session.submit_audio(signal) is SubmitResult.CLOSED
    assert session._audio.pop() == (None, 0)


@pytest.mark.parametrize("resumed", [False, True])
def test_media_started_rechecks_exact_anchor_before_fresh_or_resumed_player(
    resumed: bool,
) -> None:
    player = _RecordingPlayer()
    if resumed:
        player.begin(1)
        player.events.clear()

    anchor_calls: list[RealtimeConfig] = []

    def reconcile(config: RealtimeConfig) -> bool:
        assert player.events == []
        anchor_calls.append(config)
        return False

    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        clock=lambda: 10.0,
        aec_verifier=lambda _config: None,
        anchor_reconciler=reconcile,
    )
    state = session_module._DirectPlaybackState(
        newest_generation=1 if resumed else 0,
        retired_generation=1 if resumed else 0,
    )
    generation = 2 if resumed else 1

    assert session._handle_direct_lifecycle(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": generation},
        ),
        _FakeSidecar(),
        player,
        state,
    )

    assert anchor_calls == [session._config]
    assert player.events == [("resume" if resumed else "begin", generation)]
    assert state.active_generation == generation
    assert session._local_output_epoch == generation
    expected_settle = (
        0.0
        if resumed
        else 10.0 + session_module._LOCAL_BARGE_IN_PLAYBACK_SETTLE_SECONDS
    )
    assert session._local_barge_in_settle_until == pytest.approx(expected_settle)


def test_media_started_arms_transition_before_probe_without_no_drift_settle() -> None:
    player = _RecordingPlayer()
    player.begin(1)
    player.events.clear()
    session_holder: list[RealtimeSession] = []

    def reconcile(_config: RealtimeConfig) -> bool:
        assert session_holder[0]._local_anchor_transition.is_set()
        return False

    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        clock=lambda: 10.0,
        aec_verifier=lambda _config: None,
        anchor_reconciler=reconcile,
    )
    session_holder.append(session)
    session._local_barge_in_settle_until = 7.0
    state = session_module._DirectPlaybackState(
        newest_generation=1,
        retired_generation=1,
    )

    assert session._handle_direct_lifecycle(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 2},
        ),
        _FakeSidecar(),
        player,
        state,
    )

    assert player.events == [("resume", 2)]
    assert session._local_barge_in_settle_until == 7.0
    assert not session._local_anchor_transition.is_set()


@pytest.mark.parametrize("resumed", [False, True])
def test_media_started_repairs_anchor_with_correct_model_boundary(
    resumed: bool,
) -> None:
    player = _RecordingPlayer()
    if resumed:
        player.begin(1)
        player.events.clear()
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        clock=lambda: 20.0,
        aec_verifier=lambda _config: None,
        anchor_reconciler=lambda _config: True,
    )
    fir_before: tuple[float, ...] | None = None
    if resumed:
        guard, _features = _calibrated_render_echo_guard()
        session._render_echo_guard = guard
        session._set_local_output_epoch(None)
        fir_before = tuple(guard._fir)
    state = session_module._DirectPlaybackState(
        newest_generation=1 if resumed else 0,
        retired_generation=1 if resumed else 0,
    )
    generation = 2 if resumed else 1

    assert session._handle_direct_lifecycle(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": generation},
        ),
        _FakeSidecar(),
        player,
        state,
    )

    assert player.events == [("resume" if resumed else "begin", generation)]
    expected_settle = 20.0 + session_module._LOCAL_BARGE_IN_ANCHOR_REPAIR_SETTLE_SECONDS
    assert session._local_barge_in_settle_until == pytest.approx(expected_settle)
    if resumed:
        assert session._render_echo_guard is not None
        assert tuple(session._render_echo_guard._fir) == fir_before
        assert session._render_echo_guard._fir_valid_frames == 0
        assert session._render_echo_guard._stable_delay_samples is None
        assert session._local_anchor_requalification_pending


@pytest.mark.parametrize("resumed", [False, True])
def test_media_started_anchor_failure_never_starts_or_resumes_player(
    resumed: bool,
) -> None:
    player = _RecordingPlayer()
    if resumed:
        player.begin(1)
        player.events.clear()

    def fail_reconciliation(_config: RealtimeConfig) -> bool:
        raise WebSocketError("anchor unavailable")

    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        aec_verifier=lambda _config: None,
        anchor_reconciler=fail_reconciliation,
    )
    state = session_module._DirectPlaybackState(
        newest_generation=1 if resumed else 0,
        retired_generation=1 if resumed else 0,
    )

    with pytest.raises(WebSocketError, match="anchor unavailable"):
        session._handle_direct_lifecycle(
            ControlMessage(
                "lifecycle",
                {
                    "event_type": "media.started",
                    "generation": 2 if resumed else 1,
                },
            ),
            _FakeSidecar(),
            player,
            state,
        )

    assert player.events == []
    assert state.active_generation is None
    assert session._local_output_epoch is None


def test_paplay_scales_only_the_next_staged_block_after_live_volume_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    writes: list[bytes] = []
    observed: list[bytes] = []
    write_lengths = deque((2, 2, 4))
    attenuator = _PlaybackAttenuator(60)

    def write(_fd: int, value: bytes | bytearray) -> int:
        writes.append(bytes(value))
        return write_lengths.popleft()

    monkeypatch.setattr(session_module, "_PLAYER_WRITE_BYTES", 4)
    monkeypatch.setattr("os.set_blocking", lambda _fd, _blocking: None)
    monkeypatch.setattr("os.write", write)
    player = _PcmPlayer(
        8,
        pcm_transform=attenuator.scale,
        write_observer=observed.append,
        popen=lambda *_args, **_kwargs: process,
    )
    source = struct.pack("<4h", 1_000, 1_000, 2_000, 2_000)

    player.begin(1)
    player.enqueue(source)
    player.service()
    assert attenuator.request(0, ramp=False) == 0
    player.service()
    player.service()

    # The partially written first block is never transformed twice. The next
    # 20 ms staging boundary observes the new target immediately.
    assert writes == [
        struct.pack("<2h", 1_000, 1_000),
        struct.pack("<h", 1_000),
        struct.pack("<2h", 0, 0),
    ]
    assert observed == [
        struct.pack("<h", 1_000),
        struct.pack("<h", 1_000),
        struct.pack("<2h", 0, 0),
    ]
    assert not write_lengths
    player.abort()


def test_paplay_observer_ignores_blocked_and_aborted_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    observed: list[bytes] = []
    monkeypatch.setattr("os.set_blocking", lambda _fd, _blocking: None)

    def blocked_write(_fd: int, _value: bytes | bytearray) -> int:
        raise BlockingIOError

    monkeypatch.setattr("os.write", blocked_write)
    player = _PcmPlayer(
        4,
        write_observer=observed.append,
        popen=lambda *_args, **_kwargs: process,
    )
    player.begin(1)
    player.enqueue(b"\x01\x00" * 2)
    player.service()
    player.abort()

    assert observed == []


def test_paplay_rechecks_atomic_output_fence_at_actual_write_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    writes: list[bytes] = []
    output_allowed = [False]
    monkeypatch.setattr("os.set_blocking", lambda _fd, _blocking: None)
    monkeypatch.setattr(
        "os.write",
        lambda _fd, value: writes.append(bytes(value)) or len(value),
    )
    player = _PcmPlayer(
        4,
        write_allowed=lambda: output_allowed[0],
        popen=lambda *_args, **_kwargs: process,
    )
    source = b"\x01\x00" * 2
    player.begin(1)
    player.enqueue(source)

    player.service()

    assert writes == []
    assert bytes(player._staged) == source

    output_allowed[0] = True
    player.service()

    assert writes == [source]
    player.abort()


def test_default_direct_player_reports_only_attenuated_pcm_accepted_by_paplay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    writes: list[bytes] = []
    calls: list[list[str]] = []

    class VolumeController:
        def set_and_verify(self, _sink: str, _volume_percent: int) -> None:
            return

    def popen(argv: list[str], **_kwargs: object) -> _FakeProcess:
        calls.append(argv)
        return process

    monkeypatch.setattr("os.set_blocking", lambda _fd, _blocking: None)
    monkeypatch.setattr(
        "os.write", lambda _fd, value: writes.append(bytes(value)) or len(value)
    )
    session = RealtimeSession(
        _duplex_config(
            media_transport=DEVICE_WEBRTC_TRANSPORT,
            playback_volume_percent=60,
            aec_sink_volume_ceiling_percent=60,
        ),
        popen=popen,
        aec_verifier=lambda _config: None,
    )
    session._direct_diagnostics = _DirectSessionDiagnostics(started_at=0.0)
    assert session.request_playback_volume(30) == 30
    player = session._direct_player_factory(4_096, DEFAULT_PULSE_AEC_SINK)
    assert isinstance(player, _PcmPlayer)
    player._volume_controller = VolumeController()
    source = struct.pack("<2h", 4_000, -4_000)

    player.prepare()
    player.begin(1)
    player.enqueue(source)
    player.service()

    assert calls == [
        [
            *_PAPLAY_ARGV,
            f"--device={DEFAULT_PULSE_AEC_SINK}",
            "--volume=65536",
        ]
    ]
    assert writes == [struct.pack("<2h", 500, -500)]
    assert session._direct_diagnostics.playback_signal_packets == 1
    assert session._direct_diagnostics.playback_max_peak == 500
    assert session._direct_diagnostics.playback_max_rms == 500
    player.abort()


def test_paplay_staging_remains_inside_the_original_queue_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    monkeypatch.setattr(session_module, "_PLAYER_WRITE_BYTES", 4)
    monkeypatch.setattr("os.set_blocking", lambda _fd, _blocking: None)
    monkeypatch.setattr("os.write", lambda _fd, _value: 2)
    player = _PcmPlayer(4, popen=lambda *_args, **_kwargs: process)

    player.begin(1)
    player.enqueue(b"\x01\x00" * 2)
    player.service()

    with pytest.raises(WebSocketError, match="playback queue"):
        player.enqueue(b"\x02\x00" * 2)

    player.abort()


def test_paplay_rejects_a_transform_that_changes_pcm_framing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    monkeypatch.setattr("os.set_blocking", lambda _fd, _blocking: None)
    player = _PcmPlayer(
        4,
        pcm_transform=lambda value: value + b"\0\0",
        popen=lambda *_args, **_kwargs: process,
    )
    player.begin(1)
    player.enqueue(b"\x01\x00")

    with pytest.raises(WebSocketError, match="changed framing"):
        player.service()

    assert bytes(player._pending) == b"\x01\x00"
    player.abort()


def test_full_duplex_paplay_routes_only_to_configured_aec_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    calls: list[list[str]] = []
    monkeypatch.setattr("os.set_blocking", lambda _fd, _blocking: None)
    player = _PcmPlayer(
        4_096,
        sink=DEFAULT_PULSE_AEC_SINK,
        volume_percent=40,
        popen=lambda argv, **_kwargs: calls.append(argv) or process,
    )

    player.begin(1)

    assert calls == [
        [
            *_PAPLAY_ARGV,
            f"--device={DEFAULT_PULSE_AEC_SINK}",
            "--volume=26214",
        ]
    ]
    player.abort()


def test_direct_paplay_sets_exact_sink_volume_and_resumes_without_tail_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()
    calls: list[list[str]] = []

    class VolumeController:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        def set_and_verify(self, sink: str, volume_percent: int) -> None:
            self.calls.append((sink, volume_percent))

    controller = VolumeController()
    monkeypatch.setattr("os.set_blocking", lambda _fd, _blocking: None)
    player = _PcmPlayer(
        4_096,
        sink=DEFAULT_PULSE_AEC_SINK,
        volume_percent=60,
        exact_sink_volume=True,
        volume_controller=controller,
        popen=lambda argv, **_kwargs: calls.append(argv) or process,
    )

    player.prepare()
    player.begin(1)
    player.prepare()
    player.resume(2)

    assert controller.calls == [
        (DEFAULT_PULSE_AEC_SINK, 60),
    ]
    assert calls == [
        [
            *_PAPLAY_ARGV,
            f"--device={DEFAULT_PULSE_AEC_SINK}",
            "--volume=65536",
        ]
    ]
    assert player.active
    assert not process.killed
    player.abort()
    assert process.killed


def test_bridge_native_full_duplex_uses_exact_sink_software_volume_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeRealtimeConnection()
    player = _LoopPlayer()
    constructions: list[tuple[int, dict[str, object]]] = []

    def build_player(maximum_bytes: int, **kwargs: object) -> _LoopPlayer:
        constructions.append((maximum_bytes, kwargs))
        return player

    monkeypatch.setattr(session_module, "_PcmPlayer", build_player)
    monkeypatch.setattr(
        session_module,
        "_socket_readable",
        lambda transport, timeout: transport.wait_readable(timeout),
    )
    session = RealtimeSession(
        _duplex_config(
            capture_backend=NATIVE_AEC3_CAPTURE,
            playback_volume_percent=60,
            aec_sink_volume_ceiling_percent=60,
        ),
        connection_factory=lambda **_kwargs: connection,  # type: ignore[arg-type]
        aec_verifier=lambda _config: None,
    )
    assert session.request_playback_volume(30) == 30

    session.start()
    assert connection.wait_for_json(
        {
            "type": "start",
            "protocol_version": 2,
            "conversation_mode": "native",
            "audio_transport": "binary",
            "input_sample_rate": 16_000,
            "input_channels": 1,
        }
    )
    connection.feed(Message("text", json.dumps(_started())))
    assert _wait_for(lambda: session.ready)
    session.stop()
    assert session.join(1.0)

    assert len(constructions) == 1
    assert player.events[0] == ("prepare", None)
    maximum_bytes, kwargs = constructions[0]
    assert maximum_bytes == session._config.output_queue_bytes
    assert kwargs["sink"] == DEFAULT_PULSE_AEC_SINK
    assert kwargs["volume_percent"] == 60
    assert kwargs["exact_sink_volume"] is True
    assert kwargs["write_observer"] == session._observe_direct_playback_write
    assert kwargs["popen"] is session._popen
    transform = kwargs["pcm_transform"]
    assert callable(transform)
    assert transform(struct.pack("<2h", 4_000, -4_000)) == struct.pack("<2h", 500, -500)


def test_full_duplex_preflight_requires_exact_active_pulseaudio_aec_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    responses = {
        (*_PACTL_ARGV, "get-default-source"): (
            f"{DEFAULT_PULSE_AEC_SOURCE}\n".encode()
        ),
        (*_PACTL_ARGV, "get-default-sink"): (f"{DEFAULT_PULSE_AEC_SINK}\n".encode()),
        (*_PACTL_ARGV, "list", "short", "modules"): (
            b"1\tmodule-alsa-source\tdevice=hw:0,2\n"
            b"7\tmodule-echo-cancel\t"
            b"source_master=alsa_input.hw_0_2 "
            b"sink_master=alsa_output.hw_0_1 "
            b"source_name=codex_echo_cancel_source "
            b"sink_name=codex_echo_cancel_sink "
            b"aec_method=webrtc use_master_format=1\n"
        ),
        (*_PACTL_ARGV, "list", "short", "sources"): (
            b"1\talsa_input.hw_0_2\tmodule-alsa-source.c\ts16le 2ch 16000Hz\n"
            b"3\tcodex_echo_cancel_source\tmodule-echo-cancel.c\ts16le 2ch 16000Hz\n"
        ),
        (*_PACTL_ARGV, "--format=json", "list", "source-outputs"): json.dumps(
            [
                {
                    "driver": "module-echo-cancel.c",
                    "source": 1,
                    "corked": False,
                    "properties": {},
                },
                {
                    "driver": "protocol-native.c",
                    "source": 3,
                    "corked": False,
                    "properties": {"application.process.id": "6096"},
                },
            ]
        ).encode(),
        (*_PACTL_ARGV, "get-sink-volume", DEFAULT_PULSE_AEC_SINK): (
            b"Volume: front-left: 16384 / 25% / -36.12 dB, "
            b"front-right: 16384 / 25% / -36.12 dB\n"
        ),
    }

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=responses[tuple(argv)])

    monkeypatch.setattr(session_module.subprocess, "run", run)
    monkeypatch.setattr(session_module.os, "getpid", lambda: 6096)

    _verify_pulseaudio_aec(_duplex_config(io_timeout_seconds=0.75))

    assert [argv for argv, _kwargs in calls] == [
        [*_PACTL_ARGV, "get-default-source"],
        [*_PACTL_ARGV, "get-default-sink"],
        [*_PACTL_ARGV, "list", "short", "modules"],
        [*_PACTL_ARGV, "list", "short", "sources"],
        [*_PACTL_ARGV, "--format=json", "list", "source-outputs"],
        [*_PACTL_ARGV, "get-sink-volume", DEFAULT_PULSE_AEC_SINK],
    ]
    for _argv, kwargs in calls:
        assert kwargs == {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "timeout": 0.75,
            "check": False,
            "close_fds": True,
            "shell": False,
        }


def test_native_aec3_preflight_keeps_playback_topology_without_pulse_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    responses = {
        (*_PACTL_ARGV, "get-default-source"): f"{DEFAULT_PULSE_AEC_SOURCE}\n",
        (*_PACTL_ARGV, "get-default-sink"): f"{DEFAULT_PULSE_AEC_SINK}\n",
        (*_PACTL_ARGV, "list", "short", "modules"): (
            "7\tmodule-echo-cancel\t"
            "source_master=alsa_input.hw_0_2 "
            "sink_master=alsa_output.hw_0_1 "
            "source_name=codex_echo_cancel_source "
            "sink_name=codex_echo_cancel_sink "
            "aec_method=webrtc use_master_format=1\n"
        ),
        (*_PACTL_ARGV, "get-sink-volume", DEFAULT_PULSE_AEC_SINK): (
            "Volume: left: 16384 / 25% / -36.12 dB\n"
        ),
    }

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout=responses[tuple(argv)].encode()
        )

    monkeypatch.setenv("CODEX_AEC3_ACTIVE", "1")
    monkeypatch.setattr(session_module.subprocess, "run", run)

    _verify_pulseaudio_aec(
        _duplex_config(
            capture_backend=NATIVE_AEC3_CAPTURE,
            media_transport=DEVICE_WEBRTC_TRANSPORT,
        )
    )

    assert calls == [
        [*_PACTL_ARGV, "get-default-source"],
        [*_PACTL_ARGV, "get-default-sink"],
        [*_PACTL_ARGV, "list", "short", "modules"],
        [*_PACTL_ARGV, "get-sink-volume", DEFAULT_PULSE_AEC_SINK],
    ]


def test_native_aec3_preflight_requires_matching_service_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_AEC3_ACTIVE", raising=False)
    monkeypatch.setattr(
        session_module.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("pactl must not run on mismatch"),
    )

    with pytest.raises(WebSocketError, match="native AEC3 capture is not active"):
        _verify_pulseaudio_aec(
            _duplex_config(
                capture_backend=NATIVE_AEC3_CAPTURE,
                media_transport=DEVICE_WEBRTC_TRANSPORT,
            )
        )


@pytest.mark.parametrize("method", ["adrian", "speex"])
def test_full_duplex_preflight_requires_configured_aec_method(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    outputs = {
        (*_PACTL_ARGV, "get-default-source"): f"{DEFAULT_PULSE_AEC_SOURCE}\n",
        (*_PACTL_ARGV, "get-default-sink"): f"{DEFAULT_PULSE_AEC_SINK}\n",
        (*_PACTL_ARGV, "list", "short", "modules"): (
            "7\tmodule-echo-cancel\t"
            "source_master=alsa_input.hw_0_2 "
            "sink_master=alsa_output.hw_0_1 "
            "source_name=codex_echo_cancel_source "
            "sink_name=codex_echo_cancel_sink "
            f"aec_method={method} use_master_format=1\n"
        ),
        (*_PACTL_ARGV, "list", "short", "sources"): (
            "1\talsa_input.hw_0_2\tmodule-alsa-source.c\n"
            "3\tcodex_echo_cancel_source\tmodule-echo-cancel.c\n"
        ),
        (*_PACTL_ARGV, "--format=json", "list", "source-outputs"): json.dumps(
            [
                {
                    "driver": "protocol-native.c",
                    "source": 3,
                    "corked": False,
                    "properties": {"application.process.id": "4242"},
                }
            ]
        ),
        (*_PACTL_ARGV, "get-sink-volume", DEFAULT_PULSE_AEC_SINK): (
            "Volume: left: 16384 / 25% / -36.12 dB\n"
        ),
    }
    monkeypatch.setattr(
        session_module.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=outputs[tuple(argv)].encode()
        ),
    )
    monkeypatch.setattr(session_module.os, "getpid", lambda: 4242)

    _verify_pulseaudio_aec(_duplex_config(pulse_aec_method=method))

    with pytest.raises(WebSocketError, match="echo cancellation is not active"):
        _verify_pulseaudio_aec(
            _duplex_config(pulse_aec_method=DEFAULT_PULSE_AEC_METHOD)
        )


@pytest.mark.parametrize(
    "extra_argument",
    ["aec_method=speex", "source_name=codex_echo_cancel_source"],
)
def test_full_duplex_preflight_rejects_extra_or_duplicate_module_arguments(
    monkeypatch: pytest.MonkeyPatch, extra_argument: str
) -> None:
    outputs = deque(
        [
            f"{DEFAULT_PULSE_AEC_SOURCE}\n".encode(),
            f"{DEFAULT_PULSE_AEC_SINK}\n".encode(),
            (
                "7\tmodule-echo-cancel\t"
                "source_master=alsa_input.hw_0_2 "
                "sink_master=alsa_output.hw_0_1 "
                "source_name=codex_echo_cancel_source "
                "sink_name=codex_echo_cancel_sink "
                f"aec_method=webrtc use_master_format=1 {extra_argument}\n"
            ).encode(),
        ]
    )
    monkeypatch.setattr(
        session_module.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=outputs.popleft()
        ),
    )

    with pytest.raises(WebSocketError, match="echo cancellation is not active"):
        _verify_pulseaudio_aec(_duplex_config())


@pytest.mark.parametrize(
    ("source", "sink", "modules"),
    [
        ("alsa_input.hw_0_2\n", f"{DEFAULT_PULSE_AEC_SINK}\n", "aec"),
        (f"{DEFAULT_PULSE_AEC_SOURCE}\n", "alsa_output.hw_0_1\n", "aec"),
        (
            f"{DEFAULT_PULSE_AEC_SOURCE}\n",
            f"{DEFAULT_PULSE_AEC_SINK}\n",
            "raw",
        ),
        (
            f"{DEFAULT_PULSE_AEC_SOURCE}\n",
            f"{DEFAULT_PULSE_AEC_SINK}\n",
            "wrong-master",
        ),
    ],
)
def test_full_duplex_preflight_fails_closed_on_aec_topology_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    sink: str,
    modules: str,
) -> None:
    outputs = deque(
        [
            source.encode(),
            sink.encode(),
            {
                "aec": (
                    "7\tmodule-echo-cancel\t"
                    "source_master=alsa_input.hw_0_2 "
                    "sink_master=alsa_output.hw_0_1 "
                    "source_name=codex_echo_cancel_source "
                    "sink_name=codex_echo_cancel_sink "
                    "aec_method=webrtc use_master_format=1\n"
                ),
                "raw": "1\tmodule-alsa-source\tdevice=hw:0,2\n",
                "wrong-master": (
                    "7\tmodule-echo-cancel\t"
                    "source_master=other_source sink_master=other_sink "
                    "source_name=codex_echo_cancel_source "
                    "sink_name=codex_echo_cancel_sink "
                    "aec_method=webrtc use_master_format=1\n"
                ),
            }[modules].encode(),
        ]
    )
    monkeypatch.setattr(
        session_module.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=outputs.popleft()
        ),
    )

    with pytest.raises(WebSocketError, match="echo cancellation is not active"):
        _verify_pulseaudio_aec(_duplex_config())


def test_full_duplex_preflight_contains_pactl_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session_module.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, stdout=b""),
    )

    with pytest.raises(WebSocketError, match="could not be verified"):
        _verify_pulseaudio_aec(_duplex_config())


@pytest.mark.parametrize(
    ("source_outputs", "volume_output", "ceiling"),
    [
        (
            json.dumps(
                [
                    {
                        "driver": "protocol-native.c",
                        "source": 1,
                        "corked": False,
                        "properties": {"application.process.id": "4242"},
                    }
                ]
            ),
            "Volume: left: 16384 / 25% / -36.12 dB\n",
            25,
        ),
        ("[]", "Volume: left: 16384 / 25% / -36.12 dB\n", 25),
        (
            json.dumps(
                [
                    {
                        "driver": "protocol-native.c",
                        "source": 3,
                        "corked": True,
                        "properties": {"application.process.id": "4242"},
                    }
                ]
            ),
            "Volume: left: 16384 / 25% / -36.12 dB\n",
            25,
        ),
        (
            json.dumps(
                [
                    {
                        "driver": "protocol-native.c",
                        "source": 3,
                        "corked": False,
                        "properties": {"application.process.id": "9999"},
                    }
                ]
            ),
            "Volume: left: 16384 / 25% / -36.12 dB\n",
            25,
        ),
        (
            json.dumps(
                [
                    {
                        "driver": "protocol-native.c",
                        "source": 3,
                        "corked": False,
                        "properties": {"application.process.id": "4242"},
                    }
                ]
            ),
            "Volume: left: 17039 / 26% / -35.10 dB\n",
            25,
        ),
        (
            json.dumps(
                [
                    {
                        "driver": "protocol-native.c",
                        "source": 3,
                        "corked": False,
                        "properties": {"application.process.id": "4242"},
                    }
                ]
            ),
            ("Volume: left: 7864 / 12% / -55.00 dB, right: 8519 / 13% / -53.00 dB\n"),
            12,
        ),
        (
            json.dumps(
                [
                    {
                        "driver": "protocol-native.c",
                        "source": 3,
                        "corked": False,
                        "properties": {"application.process.id": "4242"},
                    }
                ]
            ),
            "Volume: unknown\n",
            25,
        ),
    ],
)
def test_full_duplex_preflight_rejects_capture_bypass_or_excess_volume(
    monkeypatch: pytest.MonkeyPatch,
    source_outputs: str,
    volume_output: str,
    ceiling: int,
) -> None:
    outputs = {
        (*_PACTL_ARGV, "get-default-source"): f"{DEFAULT_PULSE_AEC_SOURCE}\n",
        (*_PACTL_ARGV, "get-default-sink"): f"{DEFAULT_PULSE_AEC_SINK}\n",
        (*_PACTL_ARGV, "list", "short", "modules"): (
            "7\tmodule-echo-cancel\t"
            "source_master=alsa_input.hw_0_2 "
            "sink_master=alsa_output.hw_0_1 "
            "source_name=codex_echo_cancel_source "
            "sink_name=codex_echo_cancel_sink "
            "aec_method=webrtc use_master_format=1\n"
        ),
        (*_PACTL_ARGV, "list", "short", "sources"): (
            "1\talsa_input.hw_0_2\tmodule-alsa-source.c\n"
            "3\tcodex_echo_cancel_source\tmodule-echo-cancel.c\n"
        ),
        (*_PACTL_ARGV, "--format=json", "list", "source-outputs"): source_outputs,
        (*_PACTL_ARGV, "get-sink-volume", DEFAULT_PULSE_AEC_SINK): volume_output,
    }
    monkeypatch.setattr(
        session_module.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=outputs[tuple(argv)].encode()
        ),
    )
    monkeypatch.setattr(session_module.os, "getpid", lambda: 4242)

    with pytest.raises(WebSocketError, match="echo cancellation is not active"):
        _verify_pulseaudio_aec(
            _duplex_config(
                aec_sink_volume_ceiling_percent=ceiling,
                playback_volume_percent=min(
                    DEFAULT_PLAYBACK_VOLUME_PERCENT,
                    ceiling,
                ),
            )
        )


@pytest.mark.parametrize(
    ("raw_volume", "ceiling", "expected"),
    [
        (16_384, 25, True),
        (16_385, 25, False),
        (39_321, 60, True),
        (39_322, 60, False),
    ],
)
def test_sink_volume_ceiling_uses_exact_raw_pulseaudio_units(
    raw_volume: int, ceiling: int, expected: bool
) -> None:
    displayed = f"Volume: mono: {raw_volume} / {ceiling}% / -36.12 dB\n"

    assert (
        session_module._sink_volume_within_ceiling(displayed, ceiling=ceiling)
        is expected
    )


@pytest.mark.parametrize(
    ("probe", "expected_repaired", "expected_sets"),
    [
        (
            (
                "Volume: front-left: 39321 / 60% / -4.44 dB, "
                "front-right: 39321 / 60% / -4.44 dB\n"
            ),
            False,
            [],
        ),
        (
            (
                "Volume: front-left: 32768 / 50% / -6.02 dB, "
                "front-right: 32768 / 50% / -6.02 dB\n"
            ),
            True,
            [(DEFAULT_PULSE_AEC_SINK, 60)],
        ),
    ],
)
def test_exact_sink_anchor_reports_no_drift_or_repairs_every_channel(
    monkeypatch: pytest.MonkeyPatch,
    probe: str,
    expected_repaired: bool,
    expected_sets: list[tuple[str, int]],
) -> None:
    calls: list[tuple[str, int]] = []

    class Controller:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def set_and_verify(self, sink: str, volume_percent: int) -> None:
            calls.append((sink, volume_percent))

    monkeypatch.setattr(
        session_module, "_pactl_output", lambda *_args, **_kwargs: probe
    )
    monkeypatch.setattr(session_module, "PactlSinkVolumeController", Controller)

    repaired = session_module._repair_aec_sink_volume(
        _duplex_config(
            playback_volume_percent=60,
            aec_sink_volume_ceiling_percent=60,
        )
    )

    assert repaired is expected_repaired
    assert calls == expected_sets


@pytest.mark.parametrize(
    "probe",
    [
        "Volume: unknown\n",
        (
            "Volume: front-left: 39321 / 60% / -4.44 dB, "
            "front-right: 32768 / 50% / -6.02 dB\n"
        ),
    ],
)
def test_sink_anchor_malformed_or_unequal_channels_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    probe: str,
) -> None:
    monkeypatch.setattr(
        session_module, "_pactl_output", lambda *_args, **_kwargs: probe
    )

    with pytest.raises(WebSocketError, match="could not be verified"):
        session_module._repair_aec_sink_volume(
            _duplex_config(
                playback_volume_percent=60,
                aec_sink_volume_ceiling_percent=60,
            )
        )


def test_sink_anchor_probe_failure_propagates_without_attempting_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_constructed = False

    class Controller:
        def __init__(self, **_kwargs: Any) -> None:
            nonlocal controller_constructed
            controller_constructed = True

    def fail_probe(*_args: Any, **_kwargs: Any) -> str:
        raise WebSocketError("bounded pactl probe failed")

    monkeypatch.setattr(session_module, "_pactl_output", fail_probe)
    monkeypatch.setattr(session_module, "PactlSinkVolumeController", Controller)

    with pytest.raises(WebSocketError, match="bounded pactl probe failed"):
        session_module._repair_aec_sink_volume(
            _duplex_config(
                playback_volume_percent=60,
                aec_sink_volume_ceiling_percent=60,
            )
        )

    assert not controller_constructed


def test_sink_anchor_failed_repair_is_wrapped_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Controller:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def set_and_verify(self, _sink: str, _volume_percent: int) -> None:
            raise session_module.PulsePlaybackError("verification failed")

    monkeypatch.setattr(
        session_module,
        "_pactl_output",
        lambda *_args, **_kwargs: "Volume: mono: 32768 / 50% / -6.02 dB\n",
    )
    monkeypatch.setattr(session_module, "PactlSinkVolumeController", Controller)

    with pytest.raises(WebSocketError, match="could not be repaired"):
        session_module._repair_aec_sink_volume(
            _duplex_config(
                playback_volume_percent=60,
                aec_sink_volume_ceiling_percent=60,
            )
        )


def test_sink_anchor_probe_and_repair_share_one_transaction_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter((10.0, 10.01, 10.08))
    observed_probe_timeouts: list[float] = []
    controller_constructed = False

    def probe(*_args: Any, timeout: float) -> str:
        observed_probe_timeouts.append(timeout)
        return "Volume: mono: 32768 / 50% / -6.02 dB\n"

    class Controller:
        def __init__(self, **_kwargs: Any) -> None:
            nonlocal controller_constructed
            controller_constructed = True

    monkeypatch.setattr(
        session_module,
        "time",
        SimpleNamespace(monotonic=lambda: next(timestamps)),
    )
    monkeypatch.setattr(session_module, "_pactl_output", probe)
    monkeypatch.setattr(session_module, "PactlSinkVolumeController", Controller)

    with pytest.raises(WebSocketError, match="repair timed out"):
        session_module._repair_aec_sink_volume(
            _duplex_config(
                playback_volume_percent=60,
                aec_sink_volume_ceiling_percent=60,
            ),
            transaction_timeout_seconds=0.075,
        )

    assert observed_probe_timeouts == [pytest.approx(0.065)]
    assert not controller_constructed


def test_physical_and_media_anchor_checks_use_distinct_bounded_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[float] = []

    def reconcile(
        _config: RealtimeConfig,
        *,
        transaction_timeout_seconds: float,
    ) -> bool:
        timeouts.append(transaction_timeout_seconds)
        return False

    monkeypatch.setattr(session_module, "_repair_aec_sink_volume", reconcile)
    physical = RealtimeSession(_duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT))
    with physical._state_lock:
        physical._state = SessionState.READY

    assert physical.reconcile_playback_volume(20) == 20

    media = RealtimeSession(_duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT))
    assert media._handle_direct_lifecycle(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 1},
        ),
        _FakeSidecar(),
        _RecordingPlayer(),
        session_module._DirectPlaybackState(),
    )

    assert len(timeouts) == 2
    assert 0 < timeouts[0] <= session_module._PHYSICAL_ANCHOR_REPAIR_TIMEOUT_SECONDS
    assert 0 < timeouts[1] <= session_module._MEDIA_ANCHOR_REPAIR_TIMEOUT_SECONDS
    assert timeouts[0] < timeouts[1]


def test_paplay_output_queue_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess()
    monkeypatch.setattr("os.set_blocking", lambda _fd, _blocking: None)
    player = _PcmPlayer(4, popen=lambda *_args, **_kwargs: process)
    player.begin(1)

    with pytest.raises(WebSocketError, match="playback queue"):
        player.enqueue(b"\0" * 6)

    player.abort()
    assert process.killed


def test_paplay_start_and_stdin_configuration_fail_closed_and_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    player = _PcmPlayer(
        4_096,
        popen=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("missing paplay")
        ),
    )
    with pytest.raises(WebSocketError, match="could not be started"):
        player.begin(1)
    assert not player.active

    process = _FakeProcess()
    monkeypatch.setattr(
        "os.set_blocking",
        lambda _fd, _blocking: (_ for _ in ()).throw(OSError("bad fd")),
    )
    player = _PcmPlayer(4_096, popen=lambda *_args, **_kwargs: process)
    with pytest.raises(WebSocketError, match="could not be configured"):
        player.begin(1)

    assert process.stdin.closed
    assert process.terminated
    assert process.waited == 1
    assert not player.active


def test_paplay_abort_uses_immediate_kill_without_waiting_for_stuck_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubbornProcess(_FakeProcess):
        def wait(self, timeout: float) -> int:
            assert timeout > 0
            self.waited += 1
            raise subprocess.TimeoutExpired("paplay", timeout)

    process = StubbornProcess()
    monkeypatch.setattr("os.set_blocking", lambda _fd, _blocking: None)
    player = _PcmPlayer(4_096, popen=lambda *_args, **_kwargs: process)
    player.begin(1)

    player.abort()

    assert not player.active
    assert process.killed
    assert process.waited == 0


def test_speaking_epoch_exclusively_controls_playback_and_mic_gate() -> None:
    session = RealtimeSession(_config())
    player = _RecordingPlayer()

    action, epoch, last_epoch, semantic = session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"output_audio_buffer.started"}',
        ),
        player,
        output_epoch=None,
        last_output_epoch=0,
    )
    assert (action, epoch, last_epoch, semantic) == (None, None, 0, True)
    assert session.output_active is False

    action, epoch, last_epoch, semantic = session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"speaking.started","output_epoch":3}',
        ),
        player,
        output_epoch=epoch,
        last_output_epoch=last_epoch,
    )
    assert (action, epoch, last_epoch, semantic) == (None, 3, 3, True)
    assert session.output_active

    result = session._handle_message(
        Message("binary", b"\x01\x00"),
        player,
        output_epoch=epoch,
        last_output_epoch=last_epoch,
    )
    assert result == (None, 3, 3, True)

    action, epoch, last_epoch, semantic = session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"speaking.stopped","output_epoch":3}',
        ),
        player,
        output_epoch=epoch,
        last_output_epoch=last_epoch,
    )
    assert (action, epoch, last_epoch, semantic) == (None, None, 3, True)
    # The network loop clears this only after paplay has drained and exited.
    assert session.output_active
    assert player.events == [
        ("begin", 3),
        ("audio", b"\x01\x00"),
        ("finish", 3),
    ]


def test_full_duplex_speech_start_immediately_flushes_local_output_only() -> None:
    session = RealtimeSession(_duplex_config(), aec_verifier=lambda _config: None)
    with session._state_lock:
        session._state = SessionState.READY
    player = _RecordingPlayer()

    _action, epoch, last_epoch, _semantic = session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"speaking.started","output_epoch":1}',
        ),
        player,
        output_epoch=None,
        last_output_epoch=0,
    )
    assert session.output_active
    assert session.submit_audio(b"\x01\x00") is SubmitResult.ACCEPTED

    result = session._handle_message(
        Message(
            "text",
            json.dumps(
                {
                    "type": "control",
                    "event_type": "input_audio_buffer.speech_started",
                }
            ),
        ),
        player,
        output_epoch=epoch,
        last_output_epoch=last_epoch,
    )

    assert result == (None, None, 1, True)
    assert session.output_active is False
    assert session.state is SessionState.READY
    assert session.terminal is False
    assert player.events == [("begin", 1), ("abort", None)]

    # Provider frames already in flight are quarantined until the matching
    # stop or a newer monotonic speaking epoch arrives.
    assert session._handle_message(
        Message("binary", b"late"),
        player,
        output_epoch=None,
        last_output_epoch=last_epoch,
    ) == (None, None, 1, False)
    assert session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"speaking.stopped","output_epoch":1}',
        ),
        player,
        output_epoch=None,
        last_output_epoch=last_epoch,
    ) == (None, None, 1, True)
    assert session._suppressed_output_epoch is None


@pytest.mark.parametrize(
    ("capture_backend", "capture_gain_db"),
    [
        (PULSEAUDIO_AEC_CAPTURE, 0.0),
        (NATIVE_AEC3_CAPTURE, 12.0),
    ],
)
def test_local_barge_in_flushes_after_two_speech_frames_without_stopping(
    capture_backend: str,
    capture_gain_db: float,
) -> None:
    now = [10.0]
    session = RealtimeSession(
        _duplex_config(
            capture_backend=capture_backend,
            direct_capture_gain_db=capture_gain_db,
        ),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    player = _RecordingPlayer()
    _action, epoch, last_epoch, _semantic = session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"speaking.started","output_epoch":1}',
        ),
        player,
        output_epoch=None,
        last_output_epoch=0,
    )
    _force_render_matched_near_end(session)
    now[0] += session_module._LOCAL_BARGE_IN_PLAYBACK_SETTLE_SECONDS + 0.001
    speech = (1_024).to_bytes(2, "little", signed=True) * 1_024
    quiet = b"\0" * 2_048

    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.output_active
    assert session.submit_audio(quiet) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.output_active
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    # Capture only records a generation-scoped request. The network thread
    # remains the sole owner of observable playback state and the player child.
    assert session.output_active
    assert session._audio.bytes == 8_192

    assert session._flush_local_barge_in(
        player,
        output_epoch=epoch,
        last_output_epoch=last_epoch,
    ) == (None, 4)
    assert session.output_active is False
    # The utterance stays intact for provider VAD and same-session continuation.
    assert session._audio.bytes == 8_192
    assert session._suppressed_output_epoch == 1
    assert session.state is SessionState.READY
    assert session.terminal is False
    assert not session._interrupt_requested.is_set()
    assert player.events == [("begin", 1), ("abort", None)]


def test_media_quiet_preserves_qualified_barge_in_until_network_flush() -> None:
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    player = _RecordingPlayer()
    player.begin(1)
    session._set_local_output_epoch(1)
    state = session_module._DirectPlaybackState(
        active_generation=1,
        newest_generation=1,
    )
    with session._local_barge_in_lock:
        session._local_barge_in_requested_epoch = 1
        session._local_barge_in_requested_watermark = 7

    assert session._handle_direct_lifecycle(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.quiet", "generation": 1},
        ),
        _FakeSidecar(),
        player,
        state,
    )
    assert session._local_output_epoch is None
    assert session._local_retired_barge_in_epoch == 1
    assert session._local_barge_in_requested_epoch == 1
    assert session._local_barge_in_requested_watermark == 7

    # A later recorder callback after the lifecycle boundary must not erase
    # the already-qualified causal watermark, even after playback drained.
    player.abort()
    assert session.submit_audio(bytes(2_048)) is SubmitResult.ACCEPTED
    assert session._flush_local_barge_in(
        player,
        output_epoch=None,
        last_output_epoch=1,
    ) == (None, 7)
    assert session._suppressed_output_epoch == 1


def test_adjacent_quiet_and_new_media_preserve_predecessor_barge_in() -> None:
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    player = _RecordingPlayer()
    player.begin(1)
    session._set_local_output_epoch(1)
    state = session_module._DirectPlaybackState(
        active_generation=1,
        newest_generation=1,
    )
    with session._local_barge_in_lock:
        session._local_barge_in_requested_epoch = 1
        session._local_barge_in_requested_watermark = 7
    trigger = session_module._AudioPacket(
        data=b"\x01\x00" * 32,
        captured_at=10.0,
        capture_watermark=7,
        suppress_peer_epoch=1,
    )
    session._sent_capture_watermark = 7
    session._remember_direct_preroll(trigger)

    for event_type, generation in (("media.quiet", 1), ("media.started", 2)):
        assert session._handle_direct_lifecycle(
            ControlMessage(
                "lifecycle",
                {"event_type": event_type, "generation": generation},
            ),
            _FakeSidecar(),
            player,
            state,
        )

    assert state.active_generation == 2
    assert session._local_output_epoch is None
    assert not session.output_active
    assert session._local_retired_barge_in_epoch == 1
    assert session._local_barge_in_requested_epoch == 1
    assert session._local_barge_in_requested_watermark == 7
    assert player.events == [("begin", 1)]
    assert list(session._direct_preroll) == [trigger]
    assert not session._handle_direct_playback(
        PlaybackAudio(
            generation=2,
            sample_index=0,
            media_timestamp=0,
            pcm=b"new peer output",
        ),
        player,
        state,
    )
    assert all(event[0] != "audio" for event in player.events)

    # The next loop consumes the interruption before servicing queued output,
    # retires the newly begun epoch with its peer, and retains the raw trigger.
    assert session._flush_local_barge_in(
        player,
        output_epoch=state.active_generation,
        last_output_epoch=state.newest_generation,
    ) == (None, 7)
    assert session._suppressed_output_epoch == 1
    assert player.events[-1] == ("abort", None)
    state.retired_generation = max(
        state.retired_generation,
        state.active_generation or state.newest_generation,
    )
    state.active_generation = None
    session._begin_direct_rollover_capture(7)
    replay, remaining = session._audio.pop()
    assert replay == trigger
    assert replay is not None and replay.data == trigger.data
    assert replay.suppress_peer_epoch == 1
    assert remaining == 0
    assert session.state is SessionState.INTERRUPTING


def test_local_barge_in_counter_survives_faster_no_request_network_polls() -> None:
    now = [10.0]
    session = RealtimeSession(
        _duplex_config(),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    player = _RecordingPlayer()
    _action, epoch, last_epoch, _semantic = session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"speaking.started","output_epoch":1}',
        ),
        player,
        output_epoch=None,
        last_output_epoch=0,
    )
    _force_render_matched_near_end(session)
    now[0] += session_module._LOCAL_BARGE_IN_PLAYBACK_SETTLE_SECONDS + 0.001
    speech = (1_024).to_bytes(2, "little", signed=True) * 1_024

    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    # The network loop polls every 20 ms while recorder frames arrive every
    # 64 ms. Empty polls must not erase the first qualifying frame.
    for _ in range(3):
        assert session._flush_local_barge_in(
            player,
            output_epoch=epoch,
            last_output_epoch=last_epoch,
        ) == (1, None)
        assert session.output_active

    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session._flush_local_barge_in(
        player,
        output_epoch=epoch,
        last_output_epoch=last_epoch,
    ) == (None, 2)
    assert session.output_active is False
    assert session._suppressed_output_epoch == 1
    assert player.events == [("begin", 1), ("abort", None)]


def test_local_barge_request_cannot_cross_into_a_new_output_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [10.0]
    detector_entered = threading.Event()
    release_detector = threading.Event()
    second_epoch_guard_entered = threading.Event()
    guard_calls = 0

    def volume_guard(_config: RealtimeConfig) -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == 2:
            second_epoch_guard_entered.set()

    session = RealtimeSession(
        _duplex_config(),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
        volume_guard=volume_guard,
    )
    with session._state_lock:
        session._state = SessionState.READY
    player = _RecordingPlayer()
    _action, epoch, last_epoch, _semantic = session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"speaking.started","output_epoch":1}',
        ),
        player,
        output_epoch=None,
        last_output_epoch=0,
    )
    _force_render_matched_near_end(session)
    now[0] += session_module._LOCAL_BARGE_IN_PLAYBACK_SETTLE_SECONDS + 0.001
    speech = (1_024).to_bytes(2, "little", signed=True) * 1_024
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED

    def blocking_detector(_value: bytes) -> tuple[int, int]:
        detector_entered.set()
        assert release_detector.wait(1.0)
        return 1_024, 1_024

    monkeypatch.setattr(
        session_module,
        "_pcm_peak_and_rms",
        blocking_detector,
    )
    submit_results: list[SubmitResult] = []
    transition_results: list[tuple[str | None, int | None, int, bool]] = []
    submit_thread = threading.Thread(
        target=lambda: submit_results.append(session.submit_audio(speech)),
        daemon=True,
    )
    submit_thread.start()
    detector_was_entered = detector_entered.wait(1.0)

    def transition_output_epoch() -> None:
        nonlocal epoch, last_epoch
        _action, epoch, last_epoch, _semantic = session._handle_message(
            Message(
                "text",
                '{"type":"control","event_type":"speaking.stopped","output_epoch":1}',
            ),
            player,
            output_epoch=epoch,
            last_output_epoch=last_epoch,
        )
        transition_results.append(
            session._handle_message(
                Message(
                    "text",
                    '{"type":"control","event_type":"speaking.started",'
                    '"output_epoch":2}',
                ),
                player,
                output_epoch=epoch,
                last_output_epoch=last_epoch,
            )
        )

    transition_thread = threading.Thread(target=transition_output_epoch, daemon=True)
    transition_thread.start()
    try:
        assert detector_was_entered
        assert second_epoch_guard_entered.wait(1.0)
    finally:
        release_detector.set()
    submit_thread.join(1.0)
    transition_thread.join(1.0)

    assert not submit_thread.is_alive()
    assert not transition_thread.is_alive()
    assert submit_results == [SubmitResult.ACCEPTED]
    assert transition_results == [(None, 2, 2, True)]
    assert session._flush_local_barge_in(
        player,
        output_epoch=2,
        last_output_epoch=2,
    ) == (2, None)
    assert session.output_active
    assert session._suppressed_output_epoch is None
    assert player.events == [("begin", 1), ("finish", 1), ("begin", 2)]

    # Because the stale request never committed an interruption, epoch 2 must
    # accept a fresh speech edge without an artificial quiet rearm period.
    now[0] += session_module._LOCAL_BARGE_IN_PLAYBACK_SETTLE_SECONDS + 0.001
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert session._flush_local_barge_in(
        player,
        output_epoch=2,
        last_output_epoch=2,
    ) == (None, 4)
    assert session.output_active is False
    assert session._suppressed_output_epoch == 2
    assert player.events == [
        ("begin", 1),
        ("finish", 1),
        ("begin", 2),
        ("abort", None),
    ]


def test_full_duplex_rechecks_volume_ceiling_before_every_response() -> None:
    checks: list[int] = []
    config = _duplex_config(
        aec_sink_volume_ceiling_percent=60,
        playback_volume_percent=40,
    )
    assert config.aec_sink_volume_ceiling_percent == 60
    assert config.playback_volume_percent == 40
    session = RealtimeSession(
        config,
        aec_verifier=lambda _config: None,
        volume_guard=lambda config: checks.append(
            config.aec_sink_volume_ceiling_percent
        ),
    )
    player = _RecordingPlayer()

    _action, epoch, last_epoch, _semantic = session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"speaking.started","output_epoch":1}',
        ),
        player,
        output_epoch=None,
        last_output_epoch=0,
    )
    session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"speaking.stopped","output_epoch":1}',
        ),
        player,
        output_epoch=epoch,
        last_output_epoch=last_epoch,
    )
    session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"speaking.started","output_epoch":2}',
        ),
        player,
        output_epoch=None,
        last_output_epoch=last_epoch,
    )

    assert checks == [60, 60]
    assert player.events == [("begin", 1), ("finish", 1), ("begin", 2)]


def test_full_duplex_speech_start_quarantines_tail_after_speaking_stop() -> None:
    session = RealtimeSession(_duplex_config(), aec_verifier=lambda _config: None)
    player = _RecordingPlayer()
    _action, epoch, last_epoch, _semantic = session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"speaking.started","output_epoch":1}',
        ),
        player,
        output_epoch=None,
        last_output_epoch=0,
    )
    _action, epoch, last_epoch, _semantic = session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"speaking.stopped","output_epoch":1}',
        ),
        player,
        output_epoch=epoch,
        last_output_epoch=last_epoch,
    )
    assert epoch is None
    assert player.active

    assert session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"input_audio_buffer.speech_started"}',
        ),
        player,
        output_epoch=epoch,
        last_output_epoch=last_epoch,
    ) == (None, None, 1, True)
    assert session._suppressed_output_epoch == 1
    assert session._handle_message(
        Message("binary", b"late"),
        player,
        output_epoch=None,
        last_output_epoch=last_epoch,
    ) == (None, None, 1, False)


def test_stale_output_epoch_is_quarantined() -> None:
    session = RealtimeSession(_config())
    player = _RecordingPlayer()
    result = session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"speaking.started","output_epoch":2}',
        ),
        player,
        output_epoch=None,
        last_output_epoch=4,
    )

    assert result == (None, None, 4, False)
    assert not player.events


def test_started_requires_explicit_native_conversation_mode() -> None:
    _validate_started(_started())

    without_mode = _started()
    without_mode.pop("conversation_mode")
    with pytest.raises(WebSocketError, match="incompatible realtime protocol"):
        _validate_started(without_mode)

    wrong_mode = _started()
    wrong_mode["conversation_mode"] = "managed"
    with pytest.raises(WebSocketError, match="incompatible realtime protocol"):
        _validate_started(wrong_mode)


@pytest.mark.parametrize("invalid_version", [2.0, True])
def test_started_rejects_non_exact_protocol_version(invalid_version: object) -> None:
    started = _started()
    started["protocol_version"] = invalid_version

    with pytest.raises(WebSocketError, match="incompatible realtime protocol"):
        _validate_started(started)


@pytest.mark.parametrize("invalid_version", [3.0, True])
def test_direct_answer_rejects_non_exact_protocol_version(
    invalid_version: object,
) -> None:
    answer = {
        "type": "answer",
        "protocol_version": invalid_version,
        "transport": {"type": "webrtc", "sdp": "v=0\r\n"},
    }

    with pytest.raises(WebSocketError, match="incompatible WebRTC answer"):
        _direct_answer_sdp(answer)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("protocol_version", 3.0),
        ("protocol_version", True),
        ("epoch", 1.0),
        ("epoch", True),
    ],
)
def test_direct_rollover_answer_rejects_non_exact_integers(
    field: str,
    invalid_value: object,
) -> None:
    answer = {
        "type": "rollover_answer",
        "protocol_version": 3,
        "epoch": 1,
        "transport": {"type": "webrtc", "sdp": "v=0\r\n"},
    }
    answer[field] = invalid_value

    with pytest.raises(WebSocketError, match="incompatible rollover answer"):
        _direct_rollover_answer_sdp(answer, epoch=1)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("protocol_version", 3.0),
        ("protocol_version", True),
        ("epoch", 1.0),
        ("epoch", True),
    ],
)
def test_direct_rollover_started_rejects_non_exact_integers(
    field: str,
    invalid_value: object,
) -> None:
    started = {
        "type": "rollover_started",
        "protocol_version": 3,
        "epoch": 1,
        "context_retained": True,
    }
    started[field] = invalid_value

    with pytest.raises(WebSocketError, match="incompatible rollover start"):
        _direct_rollover_context_retained(started, epoch=1)


@pytest.mark.parametrize("invalid_version", [3.0, True])
def test_direct_started_rejects_non_exact_protocol_version(
    invalid_version: object,
) -> None:
    started = _direct_started()
    started["protocol_version"] = invalid_version

    with pytest.raises(WebSocketError, match="incompatible direct WebRTC protocol"):
        _validate_direct_started(started)


def test_started_requires_local_only_cancel_semantics() -> None:
    _validate_started(_started())

    with pytest.raises(WebSocketError, match="cancel semantics"):
        _validate_started(_started(remote_cancel=True))

    without_same_session_ack = _started()
    capabilities = without_same_session_ack["capabilities"]
    assert isinstance(capabilities, dict)
    capabilities.pop("same_session_interrupt_ack")
    with pytest.raises(WebSocketError, match="same-session interrupt"):
        _validate_started(without_same_session_ack)

    without_server_media = _started()
    capabilities = without_server_media["capabilities"]
    assert isinstance(capabilities, dict)
    capabilities.pop("server_owned_media")
    with pytest.raises(WebSocketError, match="does not own"):
        _validate_started(without_server_media)

    without_end_control = _started()
    capabilities = without_end_control["capabilities"]
    assert isinstance(capabilities, dict)
    capabilities.pop("native_end_conversation")
    with pytest.raises(WebSocketError, match="end-conversation"):
        _validate_started(without_end_control)


def test_interrupt_is_idempotent_closes_admission_and_forces_fresh_object() -> None:
    session = RealtimeSession(_config())
    with session._state_lock:
        session._state = SessionState.READY
    session._output_active.set()

    session.interrupt()
    session.interrupt()

    # The caller closes admission immediately; the network thread owns the
    # subsequent player abort and observable playback-state transition.
    assert session.output_active
    assert session.state is SessionState.STOPPING
    assert session.submit_audio(b"\0\0") is SubmitResult.CLOSED
    assert session._interrupt_requested.is_set()
    with pytest.raises(RuntimeError, match="already been started"):
        session.start()


@pytest.mark.parametrize("request_name", ["interrupt", "stop"])
def test_stopping_transition_is_atomic_with_audio_admission_and_clear(
    monkeypatch: pytest.MonkeyPatch, request_name: str
) -> None:
    session = RealtimeSession(_config())
    with session._state_lock:
        session._state = SessionState.READY
    put_entered = threading.Event()
    release_put = threading.Event()
    request_started = threading.Event()
    request_returned = threading.Event()
    original_put = session._audio.put

    def blocking_put(packet: Any) -> bool:
        put_entered.set()
        assert release_put.wait(1.0)
        return original_put(packet)

    monkeypatch.setattr(session._audio, "put", blocking_put)
    submit_results: list[SubmitResult] = []
    submit_thread = threading.Thread(
        target=lambda: submit_results.append(session.submit_audio(b"a" * 2_048)),
        daemon=True,
    )
    submit_thread.start()
    assert put_entered.wait(1.0)

    def request_stop() -> None:
        request_started.set()
        getattr(session, request_name)()
        request_returned.set()

    request_thread = threading.Thread(target=request_stop, daemon=True)
    request_thread.start()
    # The transition shares the admission lock, so it cannot clear the queue
    # before the in-flight put has linearized.
    try:
        assert request_started.wait(1.0)
        assert not request_returned.wait(0.05)
    finally:
        release_put.set()
    submit_thread.join(1.0)
    request_thread.join(1.0)

    assert not submit_thread.is_alive()
    assert not request_thread.is_alive()
    assert submit_results == [SubmitResult.ACCEPTED]
    assert request_returned.is_set()
    assert session.state is SessionState.STOPPING
    assert session._audio.bytes == 0
    assert session.submit_audio(b"b" * 2_048) is SubmitResult.CLOSED


@pytest.mark.parametrize("request_name", ["interrupt", "stop"])
@pytest.mark.parametrize("media_path", ["direct", "bridge"])
def test_explicit_boundary_cannot_overtake_a_dequeued_audio_send(
    monkeypatch: pytest.MonkeyPatch,
    request_name: str,
    media_path: str,
) -> None:
    config = (
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT)
        if media_path == "direct"
        else _config()
    )
    session = RealtimeSession(config, aec_verifier=lambda _config: None)
    with session._state_lock:
        session._state = SessionState.READY
    frame = b"s" * 2_048
    assert session.submit_audio(frame) is SubmitResult.ACCEPTED

    pop_entered = threading.Event()
    release_pop = threading.Event()
    original_pop = session._audio.pop

    def blocking_pop() -> Any:
        packet, remaining = original_pop()
        assert packet is not None
        pop_entered.set()
        assert release_pop.wait(1.0)
        return packet, remaining

    monkeypatch.setattr(session._audio, "pop", blocking_pop)
    sidecar = _FakeSidecar()
    connection = _FakeRealtimeConnection()

    def send_once() -> None:
        if media_path == "direct":
            session._send_direct_audio(
                sidecar,  # type: ignore[arg-type]
                _AudioPacer(),
                peer_epoch=1,
                sample_index=0,
                now=time.monotonic(),
                capture_ages_ms=deque(),
            )
        else:
            session._send_bridge_audio(connection)  # type: ignore[arg-type]

    def sent_count() -> int:
        if media_path == "direct":
            return len(sidecar.audio)
        return len(connection.binary_sent)

    send_thread = threading.Thread(target=send_once, daemon=True)
    send_thread.start()
    assert pop_entered.wait(1.0)

    boundary_started = threading.Event()
    boundary_returned = threading.Event()
    sends_seen_at_boundary: list[int] = []

    def request_boundary() -> None:
        boundary_started.set()
        getattr(session, request_name)()
        sends_seen_at_boundary.append(sent_count())
        boundary_returned.set()

    boundary_thread = threading.Thread(target=request_boundary, daemon=True)
    boundary_thread.start()
    try:
        assert boundary_started.wait(1.0)
        # Dequeue and send share the transition lock, so the explicit boundary
        # cannot return while this already-popped packet is still held.
        assert not boundary_returned.wait(0.05)
    finally:
        release_pop.set()
    send_thread.join(1.0)
    boundary_thread.join(1.0)

    assert not send_thread.is_alive()
    assert not boundary_thread.is_alive()
    assert sends_seen_at_boundary == [1]
    assert sent_count() == 1
    assert session._audio.bytes == 0
    assert session.state is SessionState.STOPPING


def test_bridge_native_capture_gain_is_applied_once_with_pcm16_saturation() -> None:
    session = RealtimeSession(
        _duplex_config(
            capture_backend=NATIVE_AEC3_CAPTURE,
            direct_capture_gain_db=6.0,
        ),
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    source = struct.pack("<6h", 1_000, -1_000, 20_000, -20_000, 32_767, -32_768)
    connection = _FakeRealtimeConnection()

    assert session.submit_audio(source) is SubmitResult.ACCEPTED
    packet, remaining = session._send_bridge_audio(connection)  # type: ignore[arg-type]

    assert packet is not None
    assert packet.data == source
    assert remaining == 0
    assert [value for value, _sent_at in connection.binary_sent] == [
        struct.pack("<6h", 1_995, -1_995, 32_767, -32_768, 32_767, -32_768)
    ]


def test_bridge_native_next_reply_echo_is_silent_after_barge_rearm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [10.0]
    session = RealtimeSession(
        _duplex_config(
            capture_backend=NATIVE_AEC3_CAPTURE,
            direct_capture_gain_db=12.0,
            input_queue_bytes=32_768,
        ),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    player = _RecordingPlayer()
    _action, output_epoch, last_output_epoch, _semantic = session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"speaking.started","output_epoch":1}',
        ),
        player,
        output_epoch=None,
        last_output_epoch=0,
    )
    guard = session._render_echo_guard
    assert guard is not None
    assert session._local_barge_in_settle_until == pytest.approx(
        now[0] + session_module._LOCAL_BARGE_IN_PLAYBACK_SETTLE_SECONDS
    )
    now[0] += session_module._LOCAL_BARGE_IN_PLAYBACK_SETTLE_SECONDS + 0.001
    monkeypatch.setattr(
        guard,
        "classify",
        lambda *_args, **_kwargs: _EchoDecision(
            _EchoDecisionKind.NEAR_END,
            1,
            correlation_permille=200,
            delay_ms=160,
            reference_matched=True,
            interrupt_qualified=True,
        ),
    )
    user_speech = struct.pack("<1024h", *([1_000, -1_000] * 512))
    connection = _FakeRealtimeConnection()

    for _ in range(2):
        assert session.submit_audio(user_speech) is SubmitResult.ACCEPTED
    assert session._local_barge_in_requested_epoch == 1
    for _ in range(2):
        packet, _remaining = session._send_bridge_audio(connection)  # type: ignore[arg-type]
        assert packet is not None and not packet.suppress_bridge
    expected_user_pcm = session_module._apply_capture_gain_pcm16(
        user_speech,
        12.0,
    )
    assert [value for value, _sent_at in connection.binary_sent] == [
        expected_user_pcm,
        expected_user_pcm,
    ]

    output_epoch, trigger_watermark = session._flush_local_barge_in(
        player,
        output_epoch=output_epoch,
        last_output_epoch=last_output_epoch,
    )
    assert (output_epoch, trigger_watermark) == (None, 2)
    assert session._local_barge_in_rearm_required

    quiet = bytes(2_048)
    for _ in range(session_module._LOCAL_BARGE_IN_REARM_QUIET_FRAMES):
        assert session.submit_audio(quiet) is SubmitResult.ACCEPTED
        packet, _remaining = session._send_bridge_audio(connection)  # type: ignore[arg-type]
        assert packet is not None and not packet.suppress_bridge
    assert not session._local_barge_in_rearm_required

    _action, output_epoch, last_output_epoch, _semantic = session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"speaking.started","output_epoch":2}',
        ),
        player,
        output_epoch=output_epoch,
        last_output_epoch=last_output_epoch,
    )
    assert session._local_barge_in_settle_until == pytest.approx(
        now[0] + session_module._LOCAL_BARGE_IN_PLAYBACK_SETTLE_SECONDS
    )
    # The render reference is sourced from the exact software-attenuated PCM
    # accepted by paplay, not from pre-volume bridge audio.
    session._observe_direct_playback_write(struct.pack("<12h", *([2_000, -2_000] * 6)))
    assert guard._render_samples
    monkeypatch.setattr(
        guard,
        "classify",
        lambda *_args, **_kwargs: _EchoDecision(
            _EchoDecisionKind.ECHO,
            2,
            correlation_permille=990,
            delay_ms=160,
            reference_matched=True,
        ),
    )
    playback_residual = struct.pack("<1024h", *([90, -90] * 25 + [0] * 974))

    for _ in range(2):
        assert session.submit_audio(playback_residual) is SubmitResult.ACCEPTED
        packet, _remaining = session._send_bridge_audio(connection)  # type: ignore[arg-type]
        assert packet is not None and packet.data == playback_residual
        assert packet.suppress_bridge

    assert [value for value, _sent_at in connection.binary_sent[-2:]] == [
        bytes(len(playback_residual)),
        bytes(len(playback_residual)),
    ]
    assert session._local_barge_in_requested_epoch is None

    # After the fresh-epoch settle window, bounded ambiguous render evidence
    # remains self-audio on v2. It must not reach the provider or trigger the
    # direct path's four-frame fail-open rollover behavior.
    now[0] += session_module._LOCAL_BARGE_IN_PLAYBACK_SETTLE_SECONDS + 0.001
    monkeypatch.setattr(
        guard,
        "classify",
        lambda *_args, **_kwargs: _EchoDecision(
            _EchoDecisionKind.AMBIGUOUS,
            2,
            correlation_permille=500,
            delay_ms=160,
            reference_matched=True,
        ),
    )
    for _ in range(session_module._LOCAL_BARGE_IN_AMBIGUOUS_FRAMES):
        assert session.submit_audio(playback_residual) is SubmitResult.ACCEPTED
        packet, _remaining = session._send_bridge_audio(connection)  # type: ignore[arg-type]
        assert packet is not None and packet.suppress_bridge

    assert [
        value
        for value, _sent_at in connection.binary_sent[
            -session_module._LOCAL_BARGE_IN_AMBIGUOUS_FRAMES :
        ]
    ] == [bytes(len(playback_residual))] * (
        session_module._LOCAL_BARGE_IN_AMBIGUOUS_FRAMES
    )
    assert session._local_barge_in_requested_epoch is None
    assert session._flush_local_barge_in(
        player,
        output_epoch=output_epoch,
        last_output_epoch=last_output_epoch,
    ) == (2, None)
    assert session.output_active
    assert player.events == [("begin", 1), ("abort", None), ("begin", 2)]


@pytest.mark.parametrize("missing_decision", [False, True])
def test_bridge_native_unmatched_capture_during_output_is_raw_without_local_cut(
    monkeypatch: pytest.MonkeyPatch,
    missing_decision: bool,
) -> None:
    now = [20.0]
    session = RealtimeSession(
        _duplex_config(
            capture_backend=NATIVE_AEC3_CAPTURE,
            direct_capture_gain_db=12.0,
            input_queue_bytes=8_192,
        ),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    player = _RecordingPlayer()
    _action, output_epoch, last_output_epoch, _semantic = session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"speaking.started","output_epoch":1}',
        ),
        player,
        output_epoch=None,
        last_output_epoch=0,
    )
    guard = session._render_echo_guard
    assert guard is not None
    now[0] += session_module._LOCAL_BARGE_IN_PLAYBACK_SETTLE_SECONDS + 0.001
    if missing_decision:
        monkeypatch.setattr(guard, "classify", lambda *_args, **_kwargs: None)
    # With no accepted render writes, the real guard returns its untrained
    # NEAR_END result with reference_matched=False. This was the physical
    # self-interruption path: absence of proof must not count as human speech.
    capture = struct.pack("<1024h", *([1_000, -1_000] * 512))
    connection = _FakeRealtimeConnection()

    for _ in range(2):
        assert session.submit_audio(capture) is SubmitResult.ACCEPTED
        packet, _remaining = session._send_bridge_audio(connection)  # type: ignore[arg-type]
        assert packet is not None and packet.data is capture
        assert not packet.suppress_bridge

    expected_provider_pcm = session_module._apply_capture_gain_pcm16(capture, 12.0)
    assert [value for value, _sent_at in connection.binary_sent] == [
        expected_provider_pcm,
        expected_provider_pcm,
    ]
    assert session._local_barge_in_requested_epoch is None
    assert session._flush_local_barge_in(
        player,
        output_epoch=output_epoch,
        last_output_epoch=last_output_epoch,
    ) == (1, None)
    assert session.output_active
    assert player.events == [("begin", 1)]


def test_bridge_native_decorrelated_near_end_aborts_and_stays_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [20.0]
    session = RealtimeSession(
        _duplex_config(
            capture_backend=NATIVE_AEC3_CAPTURE,
            direct_capture_gain_db=12.0,
            input_queue_bytes=8_192,
        ),
        clock=lambda: now[0],
        aec_verifier=lambda _config: None,
    )
    with session._state_lock:
        session._state = SessionState.READY
    player = _RecordingPlayer()
    _action, output_epoch, last_output_epoch, _semantic = session._handle_message(
        Message(
            "text",
            '{"type":"control","event_type":"speaking.started","output_epoch":1}',
        ),
        player,
        output_epoch=None,
        last_output_epoch=0,
    )
    guard = session._render_echo_guard
    assert guard is not None
    now[0] += session_module._LOCAL_BARGE_IN_PLAYBACK_SETTLE_SECONDS + 0.001
    monkeypatch.setattr(
        guard,
        "classify",
        lambda *_args, **_kwargs: _EchoDecision(
            _EchoDecisionKind.NEAR_END,
            1,
            correlation_permille=200,
            delay_ms=160,
            reference_matched=True,
            interrupt_qualified=True,
        ),
    )
    user_speech = struct.pack("<1024h", *([1_000, -1_000] * 512))
    connection = _FakeRealtimeConnection()

    for _ in range(2):
        assert session.submit_audio(user_speech) is SubmitResult.ACCEPTED
        packet, _remaining = session._send_bridge_audio(connection)  # type: ignore[arg-type]
        assert packet is not None and packet.data is user_speech
        assert not packet.suppress_bridge

    expected_provider_pcm = session_module._apply_capture_gain_pcm16(
        user_speech,
        12.0,
    )
    assert [value for value, _sent_at in connection.binary_sent] == [
        expected_provider_pcm,
        expected_provider_pcm,
    ]
    assert session._local_barge_in_requested_watermark == 2
    assert session._flush_local_barge_in(
        player,
        output_epoch=output_epoch,
        last_output_epoch=last_output_epoch,
    ) == (None, 2)
    assert not session.output_active
    assert player.events == [("begin", 1), ("abort", None)]


def test_blocking_bridge_send_does_not_block_microphone_admission() -> None:
    session = RealtimeSession(_config())
    with session._state_lock:
        session._state = SessionState.READY
    first = b"a" * 2_048
    second = b"b" * 2_048
    assert session.submit_audio(first) is SubmitResult.ACCEPTED

    send_entered = threading.Event()
    release_send = threading.Event()
    connection = _FakeRealtimeConnection()

    def blocking_send(value: bytes) -> None:
        send_entered.set()
        assert release_send.wait(1.0)
        connection.binary_sent.append(value)

    connection.send_binary = blocking_send  # type: ignore[method-assign]
    send_thread = threading.Thread(
        target=lambda: session._send_bridge_audio(connection),  # type: ignore[arg-type]
        daemon=True,
    )
    send_thread.start()
    assert send_entered.wait(1.0)

    admitted: list[SubmitResult] = []
    admission_thread = threading.Thread(
        target=lambda: admitted.append(session.submit_audio(second)),
        daemon=True,
    )
    admission_thread.start()
    assert _wait_for(lambda: admitted == [SubmitResult.ACCEPTED])

    release_send.set()
    send_thread.join(1.0)
    admission_thread.join(1.0)
    assert not send_thread.is_alive()
    assert not admission_thread.is_alive()
    assert connection.binary_sent == [first]
    assert session._audio.bytes == len(second)

    session.stop()


def test_network_thread_runs_v2_audio_turn_and_drains_before_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeRealtimeConnection()
    player = _LoopPlayer()
    _install_fake_loop_io(monkeypatch, player)
    session = RealtimeSession(
        _config(),
        connection_factory=lambda **_kwargs: connection,  # type: ignore[arg-type]
    )
    start = {
        "type": "start",
        "protocol_version": 2,
        "conversation_mode": "native",
        "audio_transport": "binary",
        "input_sample_rate": 16_000,
        "input_channels": 1,
    }

    session.start()
    assert connection.wait_for_json(start)
    # Capture happens while the start acknowledgment is outstanding. The
    # network thread must replay these blocks at microphone speed, not burst.
    assert session.submit_audio(b"a" * 2_048) is SubmitResult.ACCEPTED
    assert session.submit_audio(b"b" * 2_048) is SubmitResult.ACCEPTED
    connection.feed(Message("text", json.dumps(_started())))

    assert _wait_for(lambda: session.ready)
    assert connection.wait_for_binary_count(2)
    assert [value for value, _sent_at in connection.binary_sent] == [
        b"a" * 2_048,
        b"b" * 2_048,
    ]
    catch_up_interval = connection.binary_sent[1][1] - connection.binary_sent[0][1]
    assert 0.025 <= catch_up_interval < 0.060

    connection.feed(
        Message(
            "text",
            json.dumps(
                {
                    "type": "control",
                    "event_type": "speaking.started",
                    "output_epoch": 1,
                }
            ),
        )
    )
    assert _wait_for(lambda: session.output_active)
    assert session.submit_audio(b"g" * 2) is SubmitResult.GATED
    connection.feed(Message("binary", b"\x01\x00\x02\x00"))
    connection.feed(
        Message(
            "text",
            json.dumps(
                {
                    "type": "control",
                    "event_type": "speaking.stopped",
                    "output_epoch": 1,
                }
            ),
        )
    )
    assert _wait_for(lambda: ("finish", 1) in player.events)
    assert session.output_active
    player.allow_drain.set()
    assert _wait_for(lambda: not session.output_active)

    session.stop()
    assert session.join(1.0)
    assert connection.wait_for_json({"type": "stop"})
    assert session.state is SessionState.STOPPED
    assert session.terminal
    assert connection.close_frames == 1
    assert connection.closed
    assert player.events[:3] == [
        ("begin", 1),
        ("audio", b"\x01\x00\x02\x00"),
        ("finish", 1),
    ]
    assert player.events[-1] == ("abort", None)


def test_network_thread_sends_optional_voice_and_prompt_only_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeRealtimeConnection()
    player = _LoopPlayer()
    _install_fake_loop_io(monkeypatch, player)
    prompt = "Responde en español de México con un acento mexicano estable."
    session = RealtimeSession(
        _config(voice="cove", prompt=prompt),
        connection_factory=lambda **_kwargs: connection,  # type: ignore[arg-type]
    )

    session.start()
    assert connection.wait_for_json(
        {
            "type": "start",
            "protocol_version": 2,
            "conversation_mode": "native",
            "audio_transport": "binary",
            "input_sample_rate": 16_000,
            "input_channels": 1,
            "voice": "cove",
            "prompt": prompt,
        }
    )
    connection.feed(Message("text", json.dumps(_started())))
    assert _wait_for(lambda: session.ready)

    session.stop()
    assert session.join(1.0)
    assert session.state is SessionState.STOPPED


def test_network_thread_startup_failure_is_terminal_before_ready(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_connect(**_kwargs: object) -> Any:
        raise OSError("secret-token must never appear in a diagnostic")

    session = RealtimeSession(
        _config(),
        connection_factory=fail_connect,
    )

    with caplog.at_level("WARNING"):
        session.start()
        assert session.join(1.0)

    assert session.state is SessionState.FAILED
    assert session.failed_before_ready
    assert session.terminal
    assert session.ready is False
    assert caplog.messages == ["ThirdReality realtime session failed"]
    assert "secret-token" not in caplog.text


def test_interrupt_waits_for_bridge_ack_then_closes_fresh_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeRealtimeConnection()
    player = _LoopPlayer()
    _install_fake_loop_io(monkeypatch, player)
    session = RealtimeSession(
        _config(io_timeout_seconds=0.2),
        connection_factory=lambda **_kwargs: connection,  # type: ignore[arg-type]
    )

    session.start()
    assert connection.wait_for_json(
        {
            "type": "start",
            "protocol_version": 2,
            "conversation_mode": "native",
            "audio_transport": "binary",
            "input_sample_rate": 16_000,
            "input_channels": 1,
        }
    )
    connection.feed(Message("text", json.dumps(_started())))
    assert _wait_for(lambda: session.ready)

    session.interrupt()
    assert connection.wait_for_json({"type": "interrupt"})
    assert not session.terminal
    connection.feed(
        Message(
            "text",
            json.dumps(
                {
                    "type": "stopped",
                    "reason": "interrupt",
                    "fresh_session_required": True,
                    "remote_cancelled": False,
                }
            ),
        )
    )

    assert session.join(1.0)
    assert session.state is SessionState.STOPPED
    assert session.terminal
    assert not session.failed_before_ready
    assert connection.json_sent.count({"type": "interrupt"}) == 1


def test_confirmed_remote_interrupt_resumes_same_socket_and_microphone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeRealtimeConnection()
    player = _LoopPlayer()
    _install_fake_loop_io(monkeypatch, player)
    session = RealtimeSession(
        _config(io_timeout_seconds=0.2),
        connection_factory=lambda **_kwargs: connection,  # type: ignore[arg-type]
    )
    session.start()
    assert connection.wait_for_json(
        {
            "type": "start",
            "protocol_version": 2,
            "conversation_mode": "native",
            "audio_transport": "binary",
            "input_sample_rate": 16_000,
            "input_channels": 1,
        }
    )
    connection.feed(Message("text", json.dumps(_started())))
    assert _wait_for(lambda: session.ready)

    session.interrupt()
    assert connection.wait_for_json({"type": "interrupt"})
    connection.feed(
        Message(
            "text",
            json.dumps({"type": "control", "event_type": "response.cancelled"}),
        )
    )
    connection.feed(
        Message(
            "text",
            json.dumps(
                {
                    "type": "stopped",
                    "reason": "interrupt",
                    "fresh_session_required": False,
                    "remote_cancelled": True,
                }
            ),
        )
    )

    assert _wait_for(lambda: session.state is SessionState.READY)
    assert session.terminal is False
    assert connection.closed is False
    assert session.submit_audio(b"\x01\x00") is SubmitResult.ACCEPTED
    assert connection.wait_for_binary_count(1)

    session.stop()
    assert session.join(1.0)
    assert connection.json_sent.count({"type": "interrupt"}) == 1
    assert connection.json_sent.count({"type": "stop"}) == 1


def test_detached_owner_closes_after_confirmed_remote_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeRealtimeConnection()
    player = _LoopPlayer()
    _install_fake_loop_io(monkeypatch, player)
    session = RealtimeSession(
        _config(io_timeout_seconds=0.2),
        connection_factory=lambda **_kwargs: connection,  # type: ignore[arg-type]
    )
    session.start()
    assert connection.wait_for_json(
        {
            "type": "start",
            "protocol_version": 2,
            "conversation_mode": "native",
            "audio_transport": "binary",
            "input_sample_rate": 16_000,
            "input_channels": 1,
        }
    )
    connection.feed(Message("text", json.dumps(_started())))
    assert _wait_for(lambda: session.ready)

    session.interrupt()
    session.interrupt(preserve_session=False)
    assert connection.wait_for_json({"type": "interrupt"})
    connection.feed(
        Message(
            "text",
            json.dumps(
                {
                    "type": "stopped",
                    "reason": "interrupt",
                    "fresh_session_required": False,
                    "remote_cancelled": True,
                }
            ),
        )
    )

    assert session.join(1.0)
    assert session.state is SessionState.STOPPED
    assert connection.json_sent.count({"type": "stop"}) == 1


def test_interrupt_ack_requires_explicit_local_only_cancel_semantics() -> None:
    session = RealtimeSession(_config())
    player = _RecordingPlayer()

    with pytest.raises(WebSocketError, match="interrupt semantics"):
        session._handle_message(
            Message(
                "text",
                json.dumps(
                    {
                        "type": "stopped",
                        "reason": "interrupt",
                        "fresh_session_required": True,
                    }
                ),
            ),
            player,
            output_epoch=None,
            last_output_epoch=0,
        )


def test_bridge_managed_interrupt_explicitly_resumes_same_socket() -> None:
    session = RealtimeSession(_config())
    player = _RecordingPlayer()

    result = session._handle_message(
        Message(
            "text",
            json.dumps(
                {
                    "type": "stopped",
                    "reason": "interrupt",
                    "fresh_session_required": False,
                    "remote_cancelled": False,
                    "continuation_safe": True,
                }
            ),
        ),
        player,
        output_epoch=None,
        last_output_epoch=3,
    )

    assert result == ("interrupt_resumed", None, 3, True)


def test_interrupt_ack_timeout_is_bounded_and_not_reported_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeRealtimeConnection()
    player = _LoopPlayer()
    _install_fake_loop_io(monkeypatch, player)
    session = RealtimeSession(
        _config(io_timeout_seconds=0.05),
        connection_factory=lambda **_kwargs: connection,  # type: ignore[arg-type]
    )

    session.start()
    assert connection.wait_for_json(
        {
            "type": "start",
            "protocol_version": 2,
            "conversation_mode": "native",
            "audio_transport": "binary",
            "input_sample_rate": 16_000,
            "input_channels": 1,
        }
    )
    connection.feed(Message("text", json.dumps(_started())))
    assert _wait_for(lambda: session.ready)

    started_at = time.monotonic()
    session.interrupt()
    assert connection.wait_for_json({"type": "interrupt"})
    assert session.join(0.5)

    assert time.monotonic() - started_at < 0.5
    assert session.state is SessionState.STOPPED
    assert session.terminal
    assert connection.json_sent.count({"type": "interrupt"}) == 1


def test_cleanup_failure_cannot_prevent_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeRealtimeConnection()
    connection.fail_close = True
    player = _LoopPlayer()
    _install_fake_loop_io(monkeypatch, player)
    session = RealtimeSession(
        _config(),
        connection_factory=lambda **_kwargs: connection,  # type: ignore[arg-type]
    )

    session.start()
    assert connection.wait_for_json(
        {
            "type": "start",
            "protocol_version": 2,
            "conversation_mode": "native",
            "audio_transport": "binary",
            "input_sample_rate": 16_000,
            "input_channels": 1,
        }
    )
    connection.feed(Message("text", json.dumps(_started())))
    assert _wait_for(lambda: session.ready)
    session.stop()

    assert session.join(1.0)
    assert session.state is SessionState.STOPPED
    assert session.terminal
    assert connection.closed
