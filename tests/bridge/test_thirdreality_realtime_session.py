from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import deque
from typing import Any

import pytest

from device.thirdreality.realtime_client import session as session_module
from device.thirdreality.realtime_client.config import (
    DEFAULT_AEC_SINK_VOLUME_CEILING_PERCENT,
    DEFAULT_PLAYBACK_VOLUME_PERCENT,
    DEFAULT_PULSE_AEC_METHOD,
    DEFAULT_PULSE_AEC_SINK,
    DEFAULT_PULSE_AEC_SOURCE,
    RealtimeConfig,
)
from device.thirdreality.realtime_client.session import (
    _PACTL_ARGV,
    _PAPLAY_ARGV,
    RealtimeSession,
    SessionState,
    SubmitResult,
    _AudioPacer,
    _pcm_has_local_barge_in_signal,
    _pcm_has_signal,
    _PcmPlayer,
    _validate_started,
    _verify_pulseaudio_aec,
)
from device.thirdreality.realtime_client.websocket import Message, WebSocketError


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
            return bool(self._incoming)

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


def test_input_queue_is_nonblocking_bounded_and_pcm_aligned() -> None:
    session = RealtimeSession(_config(input_queue_bytes=4_096))
    with session._state_lock:
        session._state = SessionState.CONNECTING

    assert session.submit_audio(b"a" * 2_048) is SubmitResult.ACCEPTED
    assert session.submit_audio(b"b" * 2_048) is SubmitResult.ACCEPTED
    assert session.submit_audio(b"c" * 2) is SubmitResult.FULL
    assert session.submit_audio(b"odd") is SubmitResult.INVALID


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
    assert process.terminated
    assert process.waited == 1


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


def test_paplay_abort_contains_terminate_kill_and_second_wait_failures(
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
    assert process.terminated
    assert process.killed
    assert process.waited == 2


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

    assert (
        session._flush_local_barge_in(
            player,
            output_epoch=epoch,
            last_output_epoch=last_epoch,
        )
        is None
    )
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
        assert (
            session._flush_local_barge_in(
                player,
                output_epoch=epoch,
                last_output_epoch=last_epoch,
            )
            == 1
        )
        assert session.output_active

    assert session.submit_audio(speech) is SubmitResult.ACCEPTED
    assert (
        session._flush_local_barge_in(
            player,
            output_epoch=epoch,
            last_output_epoch=last_epoch,
        )
        is None
    )
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
    assert (
        session._flush_local_barge_in(
            player,
            output_epoch=2,
            last_output_epoch=2,
        )
        == 2
    )
    assert session.output_active
    assert session._suppressed_output_epoch is None
    assert player.events == [("begin", 1), ("finish", 1), ("begin", 2)]


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
