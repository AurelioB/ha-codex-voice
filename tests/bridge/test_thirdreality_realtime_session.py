from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
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
    _pcm_has_local_barge_in_signal,
    _pcm_has_signal,
    _PcmPlayer,
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
        self.ipc_sent: list[tuple[str, bytes | None]] = []
        self.interruptions = 0
        self.offer_requests = 0
        self.stop_count = 0
        self.fail_stop = False
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

    def request_offer(self) -> None:
        self.offer_requests += 1
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

    def send_audio(
        self,
        pcm: bytes,
        *,
        sample_index: int,
        capture_monotonic_ns: int,
    ) -> None:
        self.audio.append((pcm, sample_index, capture_monotonic_ns))
        self.ipc_sent.append(("audio", pcm))

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


def test_shutdown_closes_idle_prewarmed_sidecar_and_blocks_rewarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecars = (_FakeSidecar(), _FakeSidecar())
    monkeypatch.setattr(
        session_module,
        "_PREWARMED_SIDECARS",
        deque(sidecars),
    )
    monkeypatch.setattr(session_module, "_SHUTTING_DOWN", False)

    session_module.shutdown_all_sessions(timeout=0.0)

    assert all(sidecar.closed for sidecar in sidecars)
    assert not session_module._PREWARMED_SIDECARS
    assert session_module.prewarm_device_webrtc() is False
    with pytest.raises(SidecarError, match="shutting down"):
        session_module._take_prewarmed_sidecar()


def test_prewarm_keeps_initial_and_first_rollover_sidecars_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = (_FakeSidecar(), _FakeSidecar())
    pending = deque(created)

    def launch() -> _FakeSidecar:
        return pending.popleft()

    monkeypatch.setattr(session_module, "_PREWARMED_SIDECARS", deque())
    monkeypatch.setattr(session_module, "_SHUTTING_DOWN", False)
    monkeypatch.setattr(
        session_module.WebRtcSidecarClient,
        "launch",
        staticmethod(launch),
    )

    assert session_module.prewarm_device_webrtc() is True
    assert tuple(session_module._PREWARMED_SIDECARS) == created
    assert session_module._take_prewarmed_sidecar() is created[0]
    assert session_module._take_prewarmed_sidecar() is created[1]
    assert not session_module._PREWARMED_SIDECARS


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
    connection = _FakeRealtimeConnection()
    sidecars: list[_FakeSidecar] = []
    factory_calls = 0

    def make_sidecar() -> _FakeSidecar:
        nonlocal factory_calls
        factory_calls += 1
        if fail_first_standby and factory_calls == 2:
            raise SidecarError("simulated prewarm failure")
        sidecar = sidecar_builder()
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
    expected_sidecars = 1 if fail_first_standby else 2
    assert _wait_for(lambda: len(sidecars) >= expected_sidecars)
    return session, connection, sidecar, player, sidecars


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
    session, connection, sidecar, _player, _sidecars = _start_direct_session(
        monkeypatch
    )
    frame = b"\x01\x00" * 1_024

    assert session.submit_audio(frame) is SubmitResult.ACCEPTED
    assert _wait_for(lambda: bool(sidecar.audio))
    sent_pcm, sample_index, captured_ns = sidecar.audio[0]
    assert sent_pcm == frame
    assert sample_index == 0
    assert captured_ns > 0
    assert not connection.binary_sent

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
            == 5
        )
    )
    ready_records = [
        message
        for priority, message in records
        if priority == session_module.syslog.LOG_INFO
        and "direct_webrtc_status=ready" in message
    ]
    assert len(ready_records) == 5
    assert {
        next(field for field in message.split() if field.startswith("record="))
        for message in ready_records
    } == {
        "record=state",
        "record=media",
        "record=levels",
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
    for event_type in decisive_events:
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
    assert len(heartbeat_records) == 5
    heartbeat_media = next(
        message for message in heartbeat_records if "record=media" in message
    )
    heartbeat_levels = next(
        message for message in heartbeat_records if "record=levels" in message
    )
    assert "capture_sent_packets=1" in heartbeat_media
    assert "capture_signal_frames=1" in heartbeat_media
    assert "playback_signal_packets=0" in heartbeat_media
    assert "capture_max_peak=500" in heartbeat_levels
    assert "capture_max_rms=500" in heartbeat_levels
    assert "playback_max_peak=0" in heartbeat_levels
    assert "playback_max_rms=0" in heartbeat_levels
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
    assert len(terminals) == 5
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
    assert "v=0" not in emitted
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
        ("direct_webrtc_status=ready", "record=events_1"),
        ("direct_webrtc_status=ready", "record=events_2"),
        ("direct_webrtc_status=terminal", "record=state"),
        ("direct_webrtc_status=terminal", "record=media"),
        ("direct_webrtc_status=terminal", "record=levels"),
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
        "playback_signal_packets",
        "playback_signal_bytes",
        "playback_max_peak",
        "playback_max_rms",
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

    assert len(records) == 5
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
        "record=events_1",
        "record=events_2",
    }
    state = next(record for record in records if "record=state" in record)
    media = next(record for record in records if "record=media" in record)
    levels = next(record for record in records if "record=levels" in record)
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
    assert old_sidecar.stop_count >= 1
    assert old_sidecar.interruptions == 0
    assert not connection.closed
    old_audio_count = len(old_sidecar.audio)

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
    replacement = sidecars[1]
    # Capture starts flowing through the offer-created replacement before the
    # bridge/provider answer exists, preserving original timestamp freshness.
    assert _wait_for(lambda: len(replacement.audio) == 4)
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
    assert _wait_for(lambda: len(replacement.audio) == 4)
    assert [packet[0] for packet in replacement.audio] == [
        before,
        speech_one,
        speech_two,
        during_handshake,
    ]
    assert [packet[1] for packet in replacement.audio] == [
        0,
        256,
        768,
        1_536,
    ]
    assert [packet[2] for packet in replacement.audio[:2]] == old_timestamps
    assert [packet[2] for packet in replacement.audio] == sorted(
        packet[2] for packet in replacement.audio
    )
    assert len(old_sidecar.audio) == old_audio_count
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
    assert len(sidecars) == 2
    assert _wait_for(lambda: old_sidecar.offer_requests == 2)

    audible_before_late_old_media = sum(event[0] == "audio" for event in player.events)
    old_sidecar.feed(
        ControlMessage(
            "lifecycle",
            {"event_type": "media.started", "generation": 2},
        )
    )
    old_sidecar.feed(
        PlaybackAudio(
            generation=2,
            sample_index=480,
            media_timestamp=0,
            pcm=b"\x09\x00" * 480,
        )
    )
    time.sleep(0.05)
    assert session.state is SessionState.READY
    assert (
        sum(event[0] == "audio" for event in player.events)
        == audible_before_late_old_media
    )

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
    assert old_sidecar.stop_count >= 1
    assert 0.0 in old_sidecar.close_timeouts
    assert old_sidecar.closed
    assert len(sidecars) == 2
    assert sidecars[1].closed
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
    assert sidecars[1].closed
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
    assert len(sidecars) == 2
    second = sidecars[1]
    assert not first.closed
    assert _wait_for(lambda: first.offer_requests == 2)
    assert second.offer_requests == 1

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
    assert len(sidecars) == 2
    assert not second.closed
    assert _wait_for(lambda: second.offer_requests == 2)

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

    replacement = sidecars[1]
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


def test_direct_recycled_sidecar_discards_retired_audio_before_stop_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session_module,
        "_socket_readable",
        lambda transport, timeout: transport.wait_readable(timeout),
    )
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        aec_verifier=lambda _config: None,
    )
    active = _FakeSidecar()
    recycled = _FakeSidecar()
    recycled.feed(
        PlaybackAudio(
            generation=1,
            sample_index=0,
            media_timestamp=0,
            pcm=b"\x01\x00" * 480,
        )
    )
    recycled.feed(ControlMessage("stopped", {}))

    standby = session._start_direct_standby(  # type: ignore[arg-type]
        active,  # type: ignore[arg-type]
        recycled=recycled,  # type: ignore[arg-type]
    )

    assert standby is not None
    assert standby.sidecar is recycled
    assert recycled.offer_requests == 1
    assert recycled.drain_messages() == [
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
    ]


@pytest.mark.parametrize(
    "messages",
    [
        [
            ControlMessage("stopped", {}),
            PlaybackAudio(
                generation=1,
                sample_index=0,
                media_timestamp=0,
                pcm=b"\x01\x00" * 480,
            ),
        ],
        [
            ControlMessage("error", {"code": "peer_failed"}),
            ControlMessage("stopped", {}),
        ],
        [ControlMessage("connected", {}), ControlMessage("stopped", {})],
    ],
)
def test_direct_recycled_sidecar_rejects_ambiguous_stop_boundary(
    monkeypatch: pytest.MonkeyPatch,
    messages: list[ControlMessage | PlaybackAudio],
) -> None:
    monkeypatch.setattr(
        session_module,
        "_socket_readable",
        lambda transport, timeout: transport.wait_readable(timeout),
    )
    session = RealtimeSession(
        _duplex_config(media_transport=DEVICE_WEBRTC_TRANSPORT),
        aec_verifier=lambda _config: None,
    )
    active = _FakeSidecar()
    recycled = _FakeSidecar()
    for message in messages:
        recycled.feed(message)

    standby = session._start_direct_standby(  # type: ignore[arg-type]
        active,  # type: ignore[arg-type]
        recycled=recycled,  # type: ignore[arg-type]
    )

    assert standby is None
    assert recycled.closed
    assert recycled.offer_requests == 0


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


def test_direct_rollover_does_not_replace_dead_standby_with_third_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, connection, active, _player, sidecars = _start_direct_session(
        monkeypatch,
    )
    standby = sidecars[1]
    assert _wait_for(lambda: not standby._incoming)
    with standby._condition:
        standby.process.returncode = 17
        standby.closed = True
        standby._condition.notify_all()
    assert _wait_for(lambda: standby.closed)

    active.feed(
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
    assert len(sidecars) == 2
    assert not any(value.get("type") == "rollover" for value in connection.json_sent)
    assert all(candidate.closed for candidate in sidecars)


def test_direct_rollover_does_not_replace_ambiguously_closed_live_standby(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, connection, active, _player, sidecars = _start_direct_session(monkeypatch)
    standby = sidecars[1]
    assert _wait_for(lambda: not standby._incoming)
    standby.feed(ControlMessage("connected", {}))
    assert _wait_for(lambda: standby.closed)
    assert standby.process.poll() is None

    active.feed(
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
    assert len(sidecars) == 2
    assert not any(value.get("type") == "rollover" for value in connection.json_sent)
    assert all(candidate.closed for candidate in sidecars)


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
    replacement = sidecars[1]
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
    replacement = sidecars[1]
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
    replacement = sidecars[1]
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
    sidecars[1].feed(
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
            sample_index=0,
            now=10.0,
            capture_ages_ms=deque(),
        )

    assert sidecar.audio == []


def test_input_activity_ignores_floor_but_keeps_long_speech_alive() -> None:
    assert not _pcm_has_signal((255).to_bytes(2, "little", signed=True) * 8)
    assert _pcm_has_signal((-256).to_bytes(2, "little", signed=True) * 8)


def test_local_barge_in_signal_requires_peak_and_sustained_energy() -> None:
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

    def record_detector(value: bytes) -> bool:
        detector_values.append(value)
        return False

    monkeypatch.setattr(
        session_module,
        "_pcm_has_local_barge_in_signal",
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


def test_local_barge_in_flushes_after_two_speech_frames_without_stopping() -> None:
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
    assert player.events == [("begin", 1), ("abort", None)]


def test_local_barge_in_counter_survives_faster_no_request_network_polls() -> None:
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
    speech = (1_024).to_bytes(2, "little", signed=True) * 1_024
    assert session.submit_audio(speech) is SubmitResult.ACCEPTED

    def blocking_detector(_value: bytes) -> bool:
        detector_entered.set()
        assert release_detector.wait(1.0)
        return True

    monkeypatch.setattr(
        session_module,
        "_pcm_has_local_barge_in_signal",
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
