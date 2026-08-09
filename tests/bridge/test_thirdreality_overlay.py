from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

_WAKEUP_HASH = "9fc5d4920ced216444adf048f0733929a3261ae47a76ed5fa2bed8061cc46697"
_FINISH_HASH = "a1544719b6fac5cff4388a5c10f0674cd295fb98c3c86e799993db1cbee2080d"
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


class _FakePlayer:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.callbacks: list[Any] = []
        self.fail_play = False

    def play(self, _sound: object, *, done_callback: Any) -> None:
        if self.fail_play:
            raise RuntimeError("private player failure")
        self.events.append("cue")
        self.callbacks.append(done_callback)

    def stop(self) -> None:
        self.events.append("stop")


class _FakeTimerHandle:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _FakeProtocolLoop:
    def __init__(self) -> None:
        self.threadsafe_callbacks: list[Any] = []
        self.timer_handles: list[_FakeTimerHandle] = []

    def is_closed(self) -> bool:
        return False

    def call_soon_threadsafe(self, callback: Any) -> None:
        self.threadsafe_callbacks.append(callback)

    def call_later(self, _delay: float, _callback: Any, *_args: Any) -> Any:
        handle = _FakeTimerHandle()
        self.timer_handles.append(handle)
        return handle


class _FakeProtocol:
    def __init__(self) -> None:
        self.events: list[str] = []
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

    def wakeup(self, _wake_word: object) -> None:
        raise AssertionError("vendor wakeup was not patched")

    def _on_wakeup_sound_finished(self, _phrase: str) -> None:
        raise AssertionError("vendor callback must remain unused")

    def send_messages(self, messages: list[_FakeRequest]) -> None:
        if messages[0].values.get("start") is False:
            self.events.append("cancel")
        else:
            if self.fail_send:
                raise RuntimeError("private send failure")
            assert messages[0].values == {
                "start": True,
                "wake_word_phrase": "okay nabu",
            }
            self.events.append("request")

    def duck(self) -> None:
        self.events.append("duck")
        if self.fail_duck:
            raise RuntimeError("private duck failure")

    def unduck(self) -> None:
        self.events.append("unduck")

    def run_end(self) -> None:
        self._is_streaming_audio = False
        self._pipeline_active = False

    def stop(self) -> None:
        self._pipeline_active = False
        self.state.tts_player.stop()

    def connection_lost(self) -> None:
        self._is_streaming_audio = False
        self._pipeline_active = False
        self.state.connected = False


_VENDOR_WAKEUP = _FakeProtocol.wakeup
_VENDOR_FINISH = _FakeProtocol._on_wakeup_sound_finished


def _load_overlay(
    monkeypatch: pytest.MonkeyPatch,
    *hashes: str,
) -> type[_FakeProtocol]:
    _FakeProtocol.wakeup = _VENDOR_WAKEUP
    _FakeProtocol._on_wakeup_sound_finished = _VENDOR_FINISH
    aioesphomeapi = ModuleType("aioesphomeapi")
    aioesphomeapi.__path__ = []  # type: ignore[attr-defined]
    api_pb2 = ModuleType("aioesphomeapi.api_pb2")
    api_pb2.VoiceAssistantRequest = _FakeRequest  # type: ignore[attr-defined]
    linux_voice_assistant = ModuleType("linux_voice_assistant")
    linux_voice_assistant.__path__ = []  # type: ignore[attr-defined]
    satellite = ModuleType("linux_voice_assistant.satellite")
    satellite.VoiceSatelliteProtocol = _FakeProtocol  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aioesphomeapi", aioesphomeapi)
    monkeypatch.setitem(sys.modules, "aioesphomeapi.api_pb2", api_pb2)
    monkeypatch.setitem(sys.modules, "linux_voice_assistant", linux_voice_assistant)
    monkeypatch.setitem(sys.modules, "linux_voice_assistant.satellite", satellite)

    values = iter(hashes)

    def fake_sha256(_value: bytes) -> Any:
        result = next(values)
        return SimpleNamespace(hexdigest=lambda: result)

    monkeypatch.setattr(hashlib, "sha256", fake_sha256)
    spec = importlib.util.spec_from_file_location("tested_sitecustomize", _OVERLAY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _FakeProtocol._overlay_module = module  # type: ignore[attr-defined]
    return _FakeProtocol


def _wake(instance: _FakeProtocol) -> Any:
    instance.wakeup(SimpleNamespace(wake_word="okay nabu"))
    assert instance.events[:3] == ["request", "duck", "cue"]
    assert not instance._is_streaming_audio
    return instance.state.tts_player.callbacks[-1]


@pytest.mark.asyncio
async def test_wake_overlay_starts_pipeline_before_cue_and_streams_after_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = _load_overlay(monkeypatch, _WAKEUP_HASH, _FINISH_HASH)
    instance = protocol()

    callback = _wake(instance)
    callback()

    assert instance._pipeline_active
    assert instance._is_streaming_audio


@pytest.mark.asyncio
async def test_wake_overlay_schedules_watchdog_on_protocol_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = _load_overlay(monkeypatch, _WAKEUP_HASH, _FINISH_HASH)
    instance = protocol()
    loop = _FakeProtocolLoop()
    instance._loop = loop
    instance._loop_thread_id = -1

    callback = _wake(instance)

    assert len(loop.threadsafe_callbacks) == 1
    assert not loop.timer_handles
    loop.threadsafe_callbacks[0]()
    assert len(loop.timer_handles) == 1
    callback()
    assert loop.timer_handles[0].cancelled
    assert instance._is_streaming_audio


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_state", ["muted", "disconnected"])
async def test_wake_overlay_rolls_back_if_state_changes_during_cue(
    monkeypatch: pytest.MonkeyPatch,
    invalid_state: str,
) -> None:
    protocol = _load_overlay(monkeypatch, _WAKEUP_HASH, _FINISH_HASH)
    instance = protocol()
    callback = _wake(instance)
    if invalid_state == "muted":
        instance.state.muted = True
    else:
        instance.state.connected = False

    callback()

    assert not instance._pipeline_active
    assert not instance._is_streaming_audio
    assert instance._codex_wake_generation is None
    if invalid_state == "muted":
        assert "cancel" in instance.events


@pytest.mark.parametrize("invalid_state", ["muted", "disconnected"])
def test_wake_overlay_rejects_invalid_state_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    invalid_state: str,
) -> None:
    protocol = _load_overlay(monkeypatch, _WAKEUP_HASH, _FINISH_HASH)
    instance = protocol()
    if invalid_state == "muted":
        instance.state.muted = True
    else:
        instance.state.connected = False

    instance.wakeup(SimpleNamespace(wake_word="okay nabu"))

    assert not instance.events
    assert not instance._pipeline_active
    assert not instance._is_streaming_audio
    assert not hasattr(instance, "_codex_wake_generation")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["send", "duck", "play"])
async def test_wake_overlay_rolls_back_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    protocol = _load_overlay(monkeypatch, _WAKEUP_HASH, _FINISH_HASH)
    instance = protocol()
    if failure == "send":
        instance.fail_send = True
    elif failure == "duck":
        instance.fail_duck = True
    else:
        instance.state.tts_player.fail_play = True

    with pytest.raises(RuntimeError, match="private"):
        instance.wakeup(SimpleNamespace(wake_word="okay nabu"))

    assert not instance._pipeline_active
    assert not instance._is_streaming_audio
    assert instance._codex_wake_generation is None
    if failure in {"send", "duck", "play"}:
        assert "cancel" in instance.events
    if failure in {"duck", "play"}:
        assert instance.events[-1] == "unduck"


@pytest.mark.parametrize("cancel", ["run_end", "stop", "connection_lost"])
@pytest.mark.asyncio
async def test_wake_overlay_ignores_callback_after_pipeline_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    cancel: str,
) -> None:
    protocol = _load_overlay(monkeypatch, _WAKEUP_HASH, _FINISH_HASH)
    instance = protocol()
    callback = _wake(instance)

    getattr(instance, cancel)()
    callback()

    assert not instance._is_streaming_audio


@pytest.mark.asyncio
async def test_wake_overlay_ignores_callback_from_replaced_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = _load_overlay(monkeypatch, _WAKEUP_HASH, _FINISH_HASH)
    instance = protocol()
    first_callback = _wake(instance)
    instance.run_end()
    second_callback = _wake(instance)

    first_callback()
    assert not instance._is_streaming_audio
    second_callback()
    assert instance._is_streaming_audio


@pytest.mark.asyncio
async def test_wake_overlay_watchdog_aborts_missing_eof_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = _load_overlay(monkeypatch, _WAKEUP_HASH, _FINISH_HASH)
    module = protocol._overlay_module  # type: ignore[attr-defined]
    monkeypatch.setattr(module, "_WAKE_CUE_WATCHDOG_SECONDS", 0.001)
    instance = protocol()

    _wake(instance)
    await asyncio.sleep(0.01)

    assert not instance._pipeline_active
    assert not instance._is_streaming_audio
    assert instance._codex_wake_generation is None
    assert "cancel" in instance.events
    assert instance.events[-1] == "unduck"


def test_wake_overlay_fails_closed_on_unknown_vendor_bytecode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = _load_overlay(monkeypatch, "unknown", _FINISH_HASH)

    assert protocol.wakeup is _VENDOR_WAKEUP
