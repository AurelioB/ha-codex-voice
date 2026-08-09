from __future__ import annotations

import hashlib
import importlib.util
import logging
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

_BASE_WAKEUP_HASH = "9fc5d4920ced216444adf048f0733929a3261ae47a76ed5fa2bed8061cc46697"
_BASE_FINISH_HASH = "a1544719b6fac5cff4388a5c10f0674cd295fb98c3c86e799993db1cbee2080d"
_TR_WAKEUP_HASH = "4aff556b90696a3b425978641a48022021b9ffd13f4176c6bed93963577df424"
_TR_LED_FIRE_HASH = "bd6ddee49d623fff2224b5ec0dfb302075d0be9ce3c245f6cf1cf993478f9efc"
_KNOWN_HASHES = (
    _BASE_WAKEUP_HASH,
    _BASE_FINISH_HASH,
    _TR_WAKEUP_HASH,
    _TR_LED_FIRE_HASH,
)
_OVERLAY_PATH = (
    Path(__file__).resolve().parents[2]
    / "device"
    / "thirdreality"
    / "latency_sitecustomize"
    / "sitecustomize.py"
)


class _FakeRequest:
    def __init__(self, **values: Any) -> None:
        self.values = values


class _FakeAudio:
    def __init__(self, *, data: bytes) -> None:
        self.data = data


class _FakePlayer:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.callbacks: list[Any] = []

    def play(self, _sound: object, *, done_callback: Any) -> None:
        self.events.append("cue")
        self.callbacks.append(done_callback)

    def stop(self) -> None:
        self.events.append("stop")


class _FakeProtocol:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.audio: list[bytes] = []
        self.state = SimpleNamespace(
            connected=True,
            muted=False,
            tts_player=_FakePlayer(self.events),
            wakeup_sound=object(),
        )
        self._timer_finished = False
        self._pipeline_active = False
        self._is_streaming_audio = False
        self.fail_send = False
        self.fail_duck = False
        self.after_send: Any = None
        self.after_duck: Any = None

    def wakeup(self, wake_word: Any) -> None:
        if self._timer_finished:
            self._timer_finished = False
            self.unduck()
            self.state.tts_player.stop()
            return
        if self.state.muted or self._pipeline_active:
            return
        self._pipeline_active = True
        self.duck()
        self.state.tts_player.play(
            self.state.wakeup_sound,
            done_callback=lambda: self._on_wakeup_sound_finished(wake_word.wake_word),
        )

    def _on_wakeup_sound_finished(self, phrase: str) -> None:
        self.send_messages([_FakeRequest(start=True, wake_word_phrase=phrase)])
        self._is_streaming_audio = True

    def send_messages(self, messages: list[Any]) -> None:
        message = messages[0]
        if isinstance(message, _FakeAudio):
            self.events.append("audio")
            self.audio.append(message.data)
            return
        if message.values.get("start") is False:
            self.events.append("cancel")
            return
        assert self._is_streaming_audio
        assert message.values == {
            "start": True,
            "wake_word_phrase": "okay nabu",
        }
        self.events.append("request")
        if self.fail_send:
            raise RuntimeError("private send failure")
        if self.after_send is not None:
            self.after_send(self)

    def duck(self) -> None:
        assert self._is_streaming_audio
        self.events.append("duck")
        if self.fail_duck:
            raise RuntimeError("private duck failure")
        if self.after_duck is not None:
            self.after_duck(self)

    def unduck(self) -> None:
        self.events.append("unduck")

    def handle_audio(self, audio_chunk: bytes) -> None:
        if not self._is_streaming_audio or self.state.muted:
            return
        self.send_messages([_FakeAudio(data=audio_chunk)])


class _FakeTRProtocol(_FakeProtocol):
    def wakeup(self, wake_word: Any) -> None:
        previous_active = self._pipeline_active
        super().wakeup(wake_word)
        if not previous_active and self._pipeline_active:
            sys.modules["thirdreality.satellite"]._led_fire("listening")  # type: ignore[attr-defined]


def _vendor_led_fire(_state: str, _to_idle: bool = False) -> None:
    """Stand-in for the blocking installed helper."""


_VENDOR_BASE_WAKEUP = _FakeProtocol.wakeup
_VENDOR_BASE_FINISH = _FakeProtocol._on_wakeup_sound_finished
_VENDOR_TR_WAKEUP = _FakeTRProtocol.wakeup


@pytest.fixture
def load_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    loaded_modules: list[ModuleType] = []

    def load(
        hashes: tuple[str, str, str, str] = _KNOWN_HASHES,
    ) -> tuple[type[_FakeTRProtocol], ModuleType, ModuleType]:
        _FakeProtocol.wakeup = _VENDOR_BASE_WAKEUP
        _FakeProtocol._on_wakeup_sound_finished = _VENDOR_BASE_FINISH
        _FakeTRProtocol.wakeup = _VENDOR_TR_WAKEUP

        aioesphomeapi = ModuleType("aioesphomeapi")
        aioesphomeapi.__path__ = []  # type: ignore[attr-defined]
        api_pb2 = ModuleType("aioesphomeapi.api_pb2")
        api_pb2.VoiceAssistantRequest = _FakeRequest  # type: ignore[attr-defined]
        linux_voice_assistant = ModuleType("linux_voice_assistant")
        linux_voice_assistant.__path__ = []  # type: ignore[attr-defined]
        base_satellite = ModuleType("linux_voice_assistant.satellite")
        base_satellite.VoiceSatelliteProtocol = _FakeProtocol  # type: ignore[attr-defined]
        thirdreality = ModuleType("thirdreality")
        thirdreality.__path__ = []  # type: ignore[attr-defined]
        tr_satellite = ModuleType("thirdreality.satellite")
        tr_satellite.TRSatelliteProtocol = _FakeTRProtocol  # type: ignore[attr-defined]
        tr_satellite._led_fire = _vendor_led_fire  # type: ignore[attr-defined]
        tr_satellite._LED_ANIMATIONS = {  # type: ignore[attr-defined]
            "listening": "active-waking.animation",
            "thinking": "active-thinking.animation",
            "idle": "active-ending.animation",
        }
        tr_satellite._ANIM_DIR = "/animations/"  # type: ignore[attr-defined]
        tr_satellite._LOGGER = logging.getLogger(  # type: ignore[attr-defined]
            f"test.thirdreality.overlay.{len(loaded_modules)}"
        )
        thirdreality.satellite = tr_satellite  # type: ignore[attr-defined]

        monkeypatch.setitem(sys.modules, "aioesphomeapi", aioesphomeapi)
        monkeypatch.setitem(sys.modules, "aioesphomeapi.api_pb2", api_pb2)
        monkeypatch.setitem(sys.modules, "linux_voice_assistant", linux_voice_assistant)
        monkeypatch.setitem(
            sys.modules,
            "linux_voice_assistant.satellite",
            base_satellite,
        )
        monkeypatch.setitem(sys.modules, "thirdreality", thirdreality)
        monkeypatch.setitem(sys.modules, "thirdreality.satellite", tr_satellite)

        values = iter(hashes)

        def fake_sha256(_value: bytes) -> Any:
            result = next(values)
            return SimpleNamespace(hexdigest=lambda: result)

        monkeypatch.setattr(hashlib, "sha256", fake_sha256)
        spec = importlib.util.spec_from_file_location(
            f"tested_sitecustomize_{len(loaded_modules)}",
            _OVERLAY_PATH,
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded_modules.append(module)

        monkeypatch.setattr(
            module.subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0,
                stderr=b"",
            ),
        )
        return _FakeTRProtocol, module, tr_satellite

    yield load

    for module in reversed(loaded_modules):
        module._shutdown_led_worker()
    _FakeProtocol.wakeup = _VENDOR_BASE_WAKEUP
    _FakeProtocol._on_wakeup_sound_finished = _VENDOR_BASE_FINISH
    _FakeTRProtocol.wakeup = _VENDOR_TR_WAKEUP


def _wake(instance: _FakeProtocol) -> None:
    instance.wakeup(SimpleNamespace(wake_word="okay nabu"))


def test_wake_fast_path_streams_immediately_without_cue_or_watchdog(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol, module, _tr_satellite = load_overlay()
    commands: list[list[str]] = []
    command_completed = threading.Event()

    def run(command: list[str], **_kwargs: Any) -> Any:
        commands.append(command)
        command_completed.set()
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", run)
    instance = protocol()

    _wake(instance)
    assert command_completed.wait(1)
    module._shutdown_led_worker()

    assert instance.events == ["request", "duck"]
    assert instance._pipeline_active
    assert instance._is_streaming_audio
    assert not instance.state.tts_player.callbacks
    assert not hasattr(instance, "_codex_wake_watchdog")
    assert not hasattr(instance, "_codex_wake_generation")
    assert len(commands) == 1
    assert commands[0][-1] == "array:string:/animations/active-waking.animation"


def test_first_post_wake_audio_is_forwarded(load_overlay: Any) -> None:
    protocol, _module, _tr_satellite = load_overlay()
    instance = protocol()

    _wake(instance)
    instance.handle_audio(b"first frame")

    assert instance.audio == [b"first frame"]
    assert instance.events[:3] == ["request", "duck", "audio"]


@pytest.mark.parametrize(
    ("failure", "expected_events"),
    [
        ("send", ["request", "cancel"]),
        ("duck", ["request", "duck", "cancel", "unduck"]),
    ],
)
def test_wake_fast_path_rolls_back_send_or_duck_failure(
    load_overlay: Any,
    failure: str,
    expected_events: list[str],
) -> None:
    protocol, _module, _tr_satellite = load_overlay()
    instance = protocol()
    instance._is_streaming_audio = True
    instance.fail_send = failure == "send"
    instance.fail_duck = failure == "duck"

    with pytest.raises(RuntimeError, match=failure):
        _wake(instance)

    assert instance.events == expected_events
    assert not instance._pipeline_active
    assert not instance._is_streaming_audio


@pytest.mark.parametrize("invalid_state", ["muted", "disconnected"])
def test_wake_fast_path_rejects_invalid_state(
    load_overlay: Any,
    invalid_state: str,
) -> None:
    protocol, _module, _tr_satellite = load_overlay()
    instance = protocol()
    if invalid_state == "muted":
        instance.state.muted = True
    else:
        instance.state.connected = False

    _wake(instance)

    assert not instance.events
    assert not instance._pipeline_active
    assert not instance._is_streaming_audio


def test_wake_fast_path_ignores_duplicate_pipeline(load_overlay: Any) -> None:
    protocol, _module, _tr_satellite = load_overlay()
    instance = protocol()
    instance._pipeline_active = True
    instance._is_streaming_audio = True

    _wake(instance)

    assert not instance.events
    assert instance._pipeline_active
    assert instance._is_streaming_audio


def test_wake_fast_path_preserves_timer_stop_guard(load_overlay: Any) -> None:
    protocol, _module, _tr_satellite = load_overlay()
    instance = protocol()
    instance._timer_finished = True

    _wake(instance)

    assert instance.events == ["unduck", "stop"]
    assert not instance._timer_finished
    assert not instance._pipeline_active
    assert not instance._is_streaming_audio


def test_wake_fast_path_clears_stale_streaming_before_start(
    load_overlay: Any,
) -> None:
    protocol, _module, _tr_satellite = load_overlay()
    instance = protocol()
    instance._is_streaming_audio = True

    _wake(instance)

    # The pinned mic thread cannot forward a frame until wakeup returns, but the
    # flag is pre-armed so asynchronous teardown can only clear it.
    assert instance.events == ["request", "duck"]
    assert instance._is_streaming_audio


@pytest.mark.parametrize("race_point", ["send", "duck"])
@pytest.mark.parametrize(
    "teardown",
    ["vad_end", "run_end", "mute", "disconnect"],
)
def test_wake_fast_path_does_not_resurrect_streaming_after_raced_teardown(
    load_overlay: Any,
    race_point: str,
    teardown: str,
) -> None:
    protocol, _module, _tr_satellite = load_overlay()
    instance = protocol()

    def invalidate(protocol_instance: _FakeProtocol) -> None:
        protocol_instance._is_streaming_audio = False
        if teardown == "mute":
            protocol_instance.state.muted = True
        elif teardown in {"run_end", "disconnect"}:
            protocol_instance._pipeline_active = False
            if teardown == "disconnect":
                protocol_instance.state.connected = False

    setattr(instance, f"after_{race_point}", invalidate)

    _wake(instance)

    assert not instance._pipeline_active
    assert not instance._is_streaming_audio
    instance.state.muted = False
    instance.state.connected = True
    instance.handle_audio(b"must not leak")
    assert not instance.audio


@pytest.mark.parametrize("mismatch_index", range(4))
def test_overlay_fails_closed_atomically_on_any_hash_mismatch(
    load_overlay: Any,
    caplog: pytest.LogCaptureFixture,
    mismatch_index: int,
) -> None:
    hashes = list(_KNOWN_HASHES)
    hashes[mismatch_index] = "unknown"

    with caplog.at_level(logging.WARNING):
        _protocol, module, tr_satellite = load_overlay(tuple(hashes))

    assert _FakeProtocol.wakeup is _VENDOR_BASE_WAKEUP
    assert tr_satellite._led_fire is _vendor_led_fire  # type: ignore[attr-defined]
    assert module._LED_WORKER is None
    assert (
        caplog.messages.count(
            "Skipping ThirdReality latency overlay: unrecognized vendor bytecode"
        )
        == 1
    )


def test_led_fire_returns_while_runner_is_blocked(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _protocol, module, tr_satellite = load_overlay()
    runner_entered = threading.Event()
    release_runner = threading.Event()
    caller_returned = threading.Event()

    def blocking_run(*_args: Any, **_kwargs: Any) -> Any:
        runner_entered.set()
        assert release_runner.wait(2)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", blocking_run)
    caller = threading.Thread(
        target=lambda: (
            tr_satellite._led_fire("listening"),  # type: ignore[attr-defined]
            caller_returned.set(),
        ),
        name="led-test-caller",
    )
    caller.start()
    try:
        assert runner_entered.wait(1)
        assert caller_returned.wait(1)
    finally:
        release_runner.set()
        caller.join(timeout=2)
        module._shutdown_led_worker()

    assert not caller.is_alive()


def test_led_worker_start_failure_is_contained_and_retryable(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _protocol, module, tr_satellite = load_overlay()

    class BrokenThread:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("cannot start worker")

    monkeypatch.setattr(module, "_create_led_worker", BrokenThread)
    with caplog.at_level(logging.WARNING):
        tr_satellite._led_fire("listening")  # type: ignore[attr-defined]

    assert module._LED_WORKER is None
    assert not module._LED_QUEUE
    assert "[led] failed to start worker for state: listening" in caplog.messages


def test_led_commands_are_serialized_in_submission_order(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _protocol, module, tr_satellite = load_overlay()
    first_entered = threading.Event()
    release_first = threading.Event()
    all_completed = threading.Event()
    commands: list[str] = []

    def ordered_run(command: list[str], **_kwargs: Any) -> Any:
        commands.append(command[-1])
        if len(commands) == 1:
            first_entered.set()
            assert release_first.wait(2)
        if len(commands) == 3:
            all_completed.set()
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", ordered_run)
    tr_satellite._led_fire("listening")  # type: ignore[attr-defined]
    tr_satellite._led_fire("thinking")  # type: ignore[attr-defined]
    tr_satellite._led_fire("idle", to_idle=True)  # type: ignore[attr-defined]
    try:
        assert first_entered.wait(1)
        assert commands == ["array:string:/animations/active-waking.animation"]
    finally:
        release_first.set()
    assert all_completed.wait(1)
    module._shutdown_led_worker()

    assert commands == [
        "array:string:/animations/active-waking.animation",
        "array:string:/animations/active-thinking.animation",
        "array:string:/animations/active-ending.animation",
    ]


def test_led_queue_coalesces_overload_to_the_newest_state(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _protocol, module, tr_satellite = load_overlay()
    first_entered = threading.Event()
    release_first = threading.Event()
    newest_completed = threading.Event()
    commands: list[str] = []

    def blocked_run(command: list[str], **_kwargs: Any) -> Any:
        commands.append(command[-1])
        if len(commands) == 1:
            first_entered.set()
            assert release_first.wait(2)
        if command[-1] == "array:string:/animations/active-ending.animation":
            newest_completed.set()
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", blocked_run)
    with caplog.at_level(logging.WARNING):
        tr_satellite._led_fire("listening")  # type: ignore[attr-defined]
        assert first_entered.wait(1)
        for _ in range(module._LED_MAX_PENDING):
            tr_satellite._led_fire("thinking")  # type: ignore[attr-defined]
        tr_satellite._led_fire("idle", to_idle=True)  # type: ignore[attr-defined]

    try:
        assert len(module._LED_QUEUE) == 1
        assert (
            caplog.messages.count(
                "[led] coalescing 8 stale states into newest state: idle"
            )
            == 1
        )
    finally:
        release_first.set()
    assert newest_completed.wait(1)
    module._shutdown_led_worker()

    assert commands == [
        "array:string:/animations/active-waking.animation",
        "array:string:/animations/active-ending.animation",
    ]


def test_led_shutdown_discards_queued_stale_animations(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _protocol, module, tr_satellite = load_overlay()
    first_entered = threading.Event()
    release_first = threading.Event()
    commands: list[str] = []

    def blocked_run(command: list[str], **_kwargs: Any) -> Any:
        commands.append(command[-1])
        first_entered.set()
        assert release_first.wait(2)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", blocked_run)
    tr_satellite._led_fire("listening")  # type: ignore[attr-defined]
    assert first_entered.wait(1)
    for state in ("thinking", "idle", "thinking"):
        tr_satellite._led_fire(state)  # type: ignore[attr-defined]

    shutdown = threading.Thread(target=module._shutdown_led_worker)
    shutdown.start()
    with module._LED_CONDITION:
        assert module._LED_CONDITION.wait_for(
            lambda: module._LED_SHUT_DOWN,
            timeout=1,
        )
    try:
        assert not module._LED_QUEUE
    finally:
        release_first.set()
        shutdown.join(timeout=2)

    assert not shutdown.is_alive()
    assert commands == ["array:string:/animations/active-waking.animation"]
    assert not module._LED_QUEUE


def test_led_worker_handles_unknown_nonzero_and_timeout(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _protocol, module, tr_satellite = load_overlay()
    outcomes: list[Any] = [
        SimpleNamespace(returncode=7, stderr=b"dbus failed\n"),
        subprocess.TimeoutExpired(cmd="dbus-send", timeout=2),
    ]
    all_attempted = threading.Event()

    def failing_run(*_args: Any, **_kwargs: Any) -> Any:
        outcome = outcomes.pop(0)
        if not outcomes:
            all_attempted.set()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(module.subprocess, "run", failing_run)
    with caplog.at_level(logging.WARNING):
        tr_satellite._led_fire("unknown")  # type: ignore[attr-defined]
        tr_satellite._led_fire("listening")  # type: ignore[attr-defined]
        tr_satellite._led_fire("thinking")  # type: ignore[attr-defined]
        assert all_attempted.wait(1)
        module._shutdown_led_worker()

    assert not outcomes
    assert "[led] unknown state: unknown" in caplog.messages
    assert "[led] dbus-send failed (rc=7): dbus failed" in caplog.messages
    assert "[led] dbus-send timeout for state: thinking" in caplog.messages


def test_led_shutdown_stops_worker_thread(
    load_overlay: Any,
) -> None:
    _protocol, module, tr_satellite = load_overlay()
    preexisting = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith(module._LED_THREAD_PREFIX)
    }
    for state in ("listening", "thinking", "idle"):
        tr_satellite._led_fire(state)  # type: ignore[attr-defined]

    module._shutdown_led_worker()
    module._shutdown_led_worker()

    assert not module._LED_QUEUE
    assert module._LED_WORKER is None
    assert {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith(module._LED_THREAD_PREFIX)
    } == preexisting
