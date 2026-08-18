from __future__ import annotations

import asyncio
import fcntl
import hashlib
import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
import time
from enum import Enum, IntEnum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from device.thirdreality.realtime_client.config import (
    DEVICE_WEBRTC_TRANSPORT,
    NATIVE_AEC3_CAPTURE,
    normalize_wake_phrase,
)
from device.thirdreality.realtime_client.config import (
    ConfigError as RealtimeConfigError,
)
from device.thirdreality.realtime_client.config import (
    load_config as load_realtime_config,
)

_BASE_WAKEUP_HASH = "9fc5d4920ced216444adf048f0733929a3261ae47a76ed5fa2bed8061cc46697"
_BASE_FINISH_HASH = "a1544719b6fac5cff4388a5c10f0674cd295fb98c3c86e799993db1cbee2080d"
_TR_WAKEUP_HASH = "4aff556b90696a3b425978641a48022021b9ffd13f4176c6bed93963577df424"
_TR_LED_FIRE_HASH = "bd6ddee49d623fff2224b5ec0dfb302075d0be9ce3c245f6cf1cf993478f9efc"
_BASE_HANDLE_AUDIO_HASH = (
    "f24c0428291b4155a3d1c8f62563f7480503d25a39b0bd7643c04546938e5b83"
)
_BASE_STOP_HASH = "46827b29f17d65de0561b8d89f36fed99c61fa5e75c6359af6545db389972f8f"
_BASE_HANDLE_MESSAGE_HASH = (
    "d930b8d7852ac6567b219119b3ac29599df0f87f39f6ad92beb9cd27cb678724"
)
_TR_INIT_HASH = "9120bc4f5b727f360bdd632bd0fef25747a299ad64aabc5ec0bd57ac299eb24b"
_BASE_INIT_HASH = "1c8edd949cc12268f15e2ead3af5d9c8125b9c22a9c74f5e7dc5a6695a3eff25"
_TR_HANDLE_MESSAGE_HASH = (
    "8795319058e8b5e353b0ea7e056e8afeceead2587d4db2e16822880e065ddb8e"
)
_TR_SYNC_VOLUME_HASH = (
    "41630a35da4a1f6dadccff70bb8bdc38acbb547c16966ba754ed33b461e73686"
)
_TR_SYNC_VOLUME_FROM_SYSTEM_HASH = (
    "dc0d3360fd0ed0750f19bdc7fb8e98b7d3e5aeb10236baa568fa09eceab5c5a4"
)
_TR_SYNC_STATE_FROM_SYSTEM_HASH = (
    "7284593c11289cad17235c5c5e0334d59bba178188e2d8b86415415e525a6843"
)
_TR_SYSTEM_SYNC_LOOP_HASH = (
    "d21c063226b22948bd34ccaf86453472a53f842821f198f7582e831b269ef0b0"
)
_TR_UPDATE_SOUND_CONFIG_HASH = (
    "17c5b751c9eb4f0e08544167c70d1f452c4fe9a33bc5c2ba3dd53b84fcbad17c"
)
_TR_INSTALL_VOLUME_BRIDGE_HASH = (
    "2c3afda093d9077d07c064a228098b428732a358bb68fc2b858559488999b833"
)
_MEDIA_PLAYER_INIT_HASH = (
    "bcd8a03dc7ca17f067b57bcb1e97aa26d7a4c6f6db64abcf844eeb1e151ee1f4"
)
_MEDIA_PLAYER_HANDLE_MESSAGE_HASH = (
    "48f2c2bbd5e6f2cb510d5e57adffa8cea1babe309310332bac8a32f1f262f1af"
)
_MEDIA_PLAYER_APPLY_VOLUME_HASH = (
    "430a758d1656600082c555fcce1c5d6ab060287526e4d9a45435438b2e358435"
)
_MEDIA_PLAYER_APPLY_VOLUME_FROM_STATE_HASH = (
    "9db2fe3d3da3a3a3dd6a49badbed27f371a4c43a74c48739d6b6a49bc916ef40"
)
_MEDIA_PLAYER_SET_VOLUME_CALLBACK_HASH = (
    "7246bd08ef78115d7a19cd3be227c00e957f76855791e5653c028e32b111ea39"
)
_MEDIA_PLAYER_GET_STATE_HASH = (
    "523a738af8686639c39ec1912597fd579090ab9f406103917c67c3d5547024eb"
)
_MEDIA_PLAYER_UPDATE_STATE_HASH = (
    "6400fc814f8299849da6ee5cdde052225aa60f6150561a4a81bf3b53f03f7e30"
)
_SERVER_STATE_PERSIST_VOLUME_HASH = (
    "ac99e6b8b49b1fdfa922c64e6d70ee46c13b3e204dc88971fb47592647a5e6ea"
)
_MAIN_MODULE_FILE_HASH = (
    "38fe14a2068eaa0bbd4af989ddc1a8581d193edcd98f1fe9a837300bec48648d"
)
_KNOWN_HASHES = (
    _BASE_WAKEUP_HASH,
    _BASE_FINISH_HASH,
    _TR_WAKEUP_HASH,
    _TR_LED_FIRE_HASH,
)
_REALTIME_HASHES = (
    *_KNOWN_HASHES,
    _BASE_HANDLE_AUDIO_HASH,
    _BASE_STOP_HASH,
    _BASE_HANDLE_MESSAGE_HASH,
    _TR_INIT_HASH,
    _BASE_INIT_HASH,
    _TR_HANDLE_MESSAGE_HASH,
    _TR_SYNC_VOLUME_HASH,
    _TR_SYNC_VOLUME_FROM_SYSTEM_HASH,
    _TR_SYNC_STATE_FROM_SYSTEM_HASH,
    _TR_SYSTEM_SYNC_LOOP_HASH,
    _TR_UPDATE_SOUND_CONFIG_HASH,
    _TR_INSTALL_VOLUME_BRIDGE_HASH,
    _MEDIA_PLAYER_INIT_HASH,
    _MEDIA_PLAYER_HANDLE_MESSAGE_HASH,
    _MEDIA_PLAYER_APPLY_VOLUME_HASH,
    _MEDIA_PLAYER_APPLY_VOLUME_FROM_STATE_HASH,
    _MEDIA_PLAYER_SET_VOLUME_CALLBACK_HASH,
    _MEDIA_PLAYER_GET_STATE_HASH,
    _MEDIA_PLAYER_UPDATE_STATE_HASH,
    _SERVER_STATE_PERSIST_VOLUME_HASH,
    _MAIN_MODULE_FILE_HASH,
)
_OVERLAY_PATH = (
    Path(__file__).resolve().parents[2]
    / "device"
    / "thirdreality"
    / "latency_sitecustomize"
    / "sitecustomize.py"
)


def _native_aec3_config(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "enabled": True,
        "url": "ws://192.0.2.10:8787/v1/realtime",
        "token": "0123456789abcdef",
        "wake_phrase": "okay computer",
        "full_duplex": True,
        "media_transport": DEVICE_WEBRTC_TRANSPORT,
        "capture_backend": NATIVE_AEC3_CAPTURE,
        "pulse_aec_source": "codex_echo_cancel_source",
        "pulse_aec_sink": "codex_echo_cancel_sink",
    }
    value.update(updates)
    return value


class _FakeRequest:
    def __init__(self, **values: Any) -> None:
        self.values = values


class _FakeAudio:
    def __init__(self, *, data: bytes) -> None:
        self.data = data


class _FakeMediaPlayerCommand(IntEnum):
    PLAY = 0
    PAUSE = 1
    STOP = 2
    MUTE = 3
    UNMUTE = 4


class _FakeSessionState(Enum):
    NEW = "new"
    CONNECTING = "connecting"
    READY = "ready"
    INTERRUPTING = "interrupting"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class _FakeMediaPlayerCommandRequest:
    def __init__(
        self,
        *,
        key: int = 17,
        has_command: bool = False,
        command: int = 0,
        has_volume: bool = False,
        volume: float = 0.0,
        has_media_url: bool = False,
    ) -> None:
        self.key = key
        self.has_command = has_command
        self.command = command
        self.has_volume = has_volume
        self.volume = volume
        self.has_media_url = has_media_url


class _FakeOutputPlayer:
    def __init__(self, name: str, events: list[str]) -> None:
        self._name = name
        self._events = events
        self.volume_calls: list[int] = []

    def set_volume(self, percent: int) -> None:
        self.volume_calls.append(percent)
        self._events.append(f"{self._name}-volume:{percent}")


class _FakeSoundConfigPath:
    def __init__(self) -> None:
        self._revision = 1

    def stat(self) -> Any:
        return SimpleNamespace(
            st_mtime_ns=self._revision,
            st_size=64,
            st_ino=17,
        )

    def mark_write(self) -> None:
        self._revision += 1


class _FakeServerState:
    def __init__(self, events: list[str], *, volume: float = 0.6) -> None:
        self._events = events
        self.volume = volume
        self.muted = False
        self.sound_config: dict[str, Any] = {"volume": round(volume * 100)}
        self.persisted_volumes: list[float] = []

    def persist_volume(self, volume: float) -> None:
        normalized = max(0.0, min(1.0, float(volume)))
        self.volume = normalized
        self.sound_config["volume"] = round(normalized * 100)
        self.persisted_volumes.append(normalized)
        self._events.append(f"persist:{normalized:.2f}")


class _FakeMediaPlayerEntity:
    def __init__(self, events: list[str], *, volume: float = 0.6) -> None:
        self.key = 17
        self.volume = volume
        self.previous_volume = volume
        self.muted = False
        self.state = "idle"
        self.music_player = _FakeOutputPlayer("music", events)
        self.announce_player = _FakeOutputPlayer("announce", events)
        self.server = SimpleNamespace(state=_FakeServerState(events))
        self._events = events
        self._on_volume_changed: Any = lambda value: self._events.append(
            f"callback:{value:.2f}"
        )

    def _get_state_message(self) -> tuple[str, Any, float, bool]:
        return ("media-state", self.state, self.volume, self.muted)

    def _update_state(self, state: Any) -> tuple[str, Any, float, bool]:
        self._events.append("state")
        self.state = state
        return self._get_state_message()

    def set_volume_callback(self, callback: Any) -> None:
        self._on_volume_changed = callback

    def apply_volume_from_state(self) -> None:
        state = self.server.state
        if bool(getattr(state, "muted", False)):
            self.previous_volume = state.volume
            return
        self._apply_volume(state.volume, persist=False)

    def _apply_volume(
        self,
        volume: float,
        *,
        persist: bool = False,
        remember: bool = True,
    ) -> None:
        normalized = max(0.0, min(1.0, float(volume)))
        percent = round(normalized * 100)
        self.music_player.set_volume(percent)
        self.announce_player.set_volume(percent)
        self.volume = normalized
        if remember:
            self.previous_volume = normalized
        if self._on_volume_changed and persist:
            self._on_volume_changed(normalized)

    def handle_message(self, message: Any) -> Any:
        if not isinstance(message, _FakeMediaPlayerCommandRequest):
            return
        if message.key != self.key or message.has_media_url:
            return
        if message.has_command:
            if message.command == _FakeMediaPlayerCommand.MUTE:
                if not self.muted:
                    self.previous_volume = self.volume
                    self.volume = 0.0
                    self.music_player.set_volume(0)
                    self.announce_player.set_volume(0)
                    self.muted = True
                yield self._update_state(self.state)
            elif message.command == _FakeMediaPlayerCommand.UNMUTE:
                if self.muted:
                    self.volume = self.previous_volume
                    percent = int(self.volume * 100)
                    self.music_player.set_volume(percent)
                    self.announce_player.set_volume(percent)
                    self.muted = False
                yield self._update_state(self.state)
            return
        if message.has_volume:
            self._apply_volume(message.volume, persist=True)
            self.server.state.persist_volume(self.volume)
            yield self._update_state(self.state)


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
    def __init__(self, state: _FakeServerState | None = None) -> None:
        self.events: list[str] = []
        self.audio: list[bytes] = []
        self.requests: list[str] = []
        self.sent_messages: list[Any] = []
        self.state = state or _FakeServerState(self.events)
        media_player_entity = getattr(self.state, "media_player_entity", None)
        if media_player_entity is None:
            media_player_entity = _FakeMediaPlayerEntity(
                self.events,
                volume=self.state.volume,
            )
        media_player_entity.server = SimpleNamespace(state=self.state)
        state_defaults = {
            "connected": True,
            "muted": False,
            "tts_player": _FakePlayer(self.events),
            "wakeup_sound": object(),
            "active_wake_words": {"okay_nabu", "okay_computer"},
            "wake_words": {
                "okay_nabu": SimpleNamespace(
                    id="okay_nabu",
                    wake_word="Okay Nabu",
                    probability_cutoff=0.85,
                    _probabilities=[],
                ),
                "okay_computer": SimpleNamespace(
                    id="okay_computer",
                    wake_word="Okay Computer",
                    probability_cutoff=0.97,
                    _probabilities=[],
                ),
            },
            "stop_word": SimpleNamespace(id="stop"),
            "media_player_entity": media_player_entity,
            "entities": [media_player_entity],
        }
        for name, value in state_defaults.items():
            if not hasattr(self.state, name):
                setattr(self.state, name, value)
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
        self.sent_messages.extend(messages)
        if isinstance(message, _FakeAudio):
            self.events.append("audio")
            self.audio.append(message.data)
            return
        if isinstance(message, tuple):
            self.events.append("send-state")
            return
        if message.values.get("start") is False:
            self.events.append("cancel")
            return
        assert self._is_streaming_audio
        assert message.values.get("start") is True
        phrase = message.values.get("wake_word_phrase")
        assert isinstance(phrase, str)
        self.requests.append(phrase)
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

    def handle_message(self, message: Any) -> Any:
        for entity in self.state.entities:
            yield from entity.handle_message(message)

    def stop(self) -> None:
        self.state.active_wake_words.discard(self.state.stop_word.id)
        self._pipeline_active = False
        if self._timer_finished:
            self._timer_finished = False
            self.unduck()
            self.state.tts_player.stop()
            return
        self.state.tts_player.stop()
        self._tts_finished()

    def _tts_finished(self) -> None:
        self.events.append("tts-finished")


class _FakeTRProtocol(_FakeProtocol):
    def __init__(self, state: _FakeServerState | None = None) -> None:
        super().__init__(state)
        self._last_system_volume: float | None = None
        self.sound_config = self.state.sound_config
        self.sound_config_updates: list[dict[str, Any]] = []
        self.fail_sound_config_update = False
        self.raise_sound_config_update = False
        self._install_volume_bridge()

    def wakeup(self, wake_word: Any) -> None:
        previous_active = self._pipeline_active
        super().wakeup(wake_word)
        if not previous_active and self._pipeline_active:
            sys.modules["thirdreality.satellite"]._led_fire("listening")  # type: ignore[attr-defined]

    def handle_message(self, message: Any) -> Any:
        yield from super().handle_message(message)

    def _install_volume_bridge(self) -> None:
        self.state.media_player_entity.set_volume_callback(self._sync_volume_to_system)

    def _sync_volume_to_system(self, volume: float) -> None:
        self.events.append(f"system-volume:{volume:.2f}")
        self._update_sound_config({"volume": round(volume * 100)})

    def _read_system_volume(self) -> float:
        return float(self.sound_config["volume"]) / 100

    def _update_sound_config(self, changes: dict[str, Any]) -> bool:
        if self.raise_sound_config_update:
            raise OSError("sound config write failed")
        if self.fail_sound_config_update:
            return False
        self.sound_config.update(changes)
        self.sound_config_updates.append(dict(changes))
        sys.modules["thirdreality.satellite"]._SOUND_CONF.mark_write()  # type: ignore[attr-defined]
        return True

    def write_physical_volume(self, percent: int) -> None:
        self.sound_config["volume"] = percent
        sys.modules["thirdreality.satellite"]._SOUND_CONF.mark_write()  # type: ignore[attr-defined]

    def _sync_volume_from_system(self, *, force: bool = False) -> None:
        normalized = self._read_system_volume()
        if (
            not force
            and self._last_system_volume is not None
            and abs(self._last_system_volume - normalized) < 0.0001
            and abs(self.state.volume - normalized) < 0.0001
        ):
            return
        self.state.volume = normalized
        entity = self.state.media_player_entity
        entity.apply_volume_from_state()
        self._last_system_volume = normalized
        self.send_messages([entity._get_state_message()])

    def _sync_muted_from_system(self, *, force: bool = False) -> None:
        del force
        self.state.media_player_entity.muted = bool(self.state.muted)

    def _sync_state_from_system(self, *, force: bool = False) -> None:
        self._sync_volume_from_system(force=force)
        self._sync_muted_from_system(force=force)

    async def _system_sync_loop(self) -> None:
        while self.state.connected:
            self._sync_state_from_system()
            await asyncio.sleep(
                sys.modules["thirdreality.satellite"]._VOLUME_POLL_INTERVAL  # type: ignore[attr-defined]
            )


def _vendor_led_fire(_state: str, _to_idle: bool = False) -> None:
    """Stand-in for the blocking installed helper."""


_VENDOR_BASE_WAKEUP = _FakeProtocol.wakeup
_VENDOR_BASE_FINISH = _FakeProtocol._on_wakeup_sound_finished
_VENDOR_BASE_HANDLE_AUDIO = _FakeProtocol.handle_audio
_VENDOR_BASE_STOP = _FakeProtocol.stop
_VENDOR_BASE_HANDLE_MESSAGE = _FakeProtocol.handle_message
_VENDOR_TR_WAKEUP = _FakeTRProtocol.wakeup
_VENDOR_TR_INIT = _FakeTRProtocol.__init__
_VENDOR_TR_HANDLE_MESSAGE = _FakeTRProtocol.handle_message
_VENDOR_TR_SYNC_VOLUME = _FakeTRProtocol._sync_volume_to_system
_VENDOR_TR_SYNC_VOLUME_FROM_SYSTEM = _FakeTRProtocol._sync_volume_from_system
_VENDOR_TR_SYNC_STATE_FROM_SYSTEM = _FakeTRProtocol._sync_state_from_system
_VENDOR_TR_SYSTEM_SYNC_LOOP = _FakeTRProtocol._system_sync_loop
_VENDOR_TR_UPDATE_SOUND_CONFIG = _FakeTRProtocol._update_sound_config
_VENDOR_TR_INSTALL_VOLUME_BRIDGE = _FakeTRProtocol._install_volume_bridge
_VENDOR_MEDIA_PLAYER_INIT = _FakeMediaPlayerEntity.__init__
_VENDOR_MEDIA_PLAYER_HANDLE_MESSAGE = _FakeMediaPlayerEntity.handle_message
_VENDOR_MEDIA_PLAYER_APPLY_VOLUME = _FakeMediaPlayerEntity._apply_volume
_VENDOR_MEDIA_PLAYER_APPLY_VOLUME_FROM_STATE = (
    _FakeMediaPlayerEntity.apply_volume_from_state
)
_VENDOR_MEDIA_PLAYER_SET_VOLUME_CALLBACK = _FakeMediaPlayerEntity.set_volume_callback
_VENDOR_MEDIA_PLAYER_GET_STATE = _FakeMediaPlayerEntity._get_state_message
_VENDOR_MEDIA_PLAYER_UPDATE_STATE = _FakeMediaPlayerEntity._update_state
_VENDOR_SERVER_STATE_PERSIST_VOLUME = _FakeServerState.persist_volume


@pytest.fixture
def load_overlay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Any:
    monkeypatch.delenv("CODEX_AEC3_ACTIVE", raising=False)
    monkeypatch.delenv("CODEX_AEC3_CAPTURE", raising=False)
    loaded_modules: list[ModuleType] = []
    vendor_main = tmp_path / "linux_voice_assistant_main.pyc"
    vendor_main.write_bytes(b"pinned vendor main")
    real_find_spec = importlib.util.find_spec

    def load(
        hashes: tuple[str, ...] = _KNOWN_HASHES,
        realtime_support: ModuleType | None = None,
        *,
        aec3_config: dict[str, object] | bytes | None = None,
        aec3_config_mode: int = 0o600,
        aec3_config_uid: int = 0,
        system_volume_poll_interval: float = 0.5,
    ) -> tuple[type[_FakeTRProtocol], ModuleType, ModuleType]:
        _FakeProtocol.wakeup = _VENDOR_BASE_WAKEUP
        _FakeProtocol._on_wakeup_sound_finished = _VENDOR_BASE_FINISH
        _FakeProtocol.handle_audio = _VENDOR_BASE_HANDLE_AUDIO
        _FakeProtocol.stop = _VENDOR_BASE_STOP
        _FakeProtocol.handle_message = _VENDOR_BASE_HANDLE_MESSAGE
        _FakeTRProtocol.wakeup = _VENDOR_TR_WAKEUP
        _FakeTRProtocol.__init__ = _VENDOR_TR_INIT
        _FakeTRProtocol.handle_message = _VENDOR_TR_HANDLE_MESSAGE
        _FakeTRProtocol._sync_volume_to_system = _VENDOR_TR_SYNC_VOLUME
        _FakeTRProtocol._sync_volume_from_system = _VENDOR_TR_SYNC_VOLUME_FROM_SYSTEM
        _FakeTRProtocol._sync_state_from_system = _VENDOR_TR_SYNC_STATE_FROM_SYSTEM
        _FakeTRProtocol._system_sync_loop = _VENDOR_TR_SYSTEM_SYNC_LOOP
        _FakeTRProtocol._update_sound_config = _VENDOR_TR_UPDATE_SOUND_CONFIG
        _FakeTRProtocol._install_volume_bridge = _VENDOR_TR_INSTALL_VOLUME_BRIDGE
        _FakeMediaPlayerEntity.__init__ = _VENDOR_MEDIA_PLAYER_INIT
        _FakeMediaPlayerEntity.handle_message = _VENDOR_MEDIA_PLAYER_HANDLE_MESSAGE
        _FakeMediaPlayerEntity._apply_volume = _VENDOR_MEDIA_PLAYER_APPLY_VOLUME
        _FakeMediaPlayerEntity.apply_volume_from_state = (
            _VENDOR_MEDIA_PLAYER_APPLY_VOLUME_FROM_STATE
        )
        _FakeMediaPlayerEntity.set_volume_callback = (
            _VENDOR_MEDIA_PLAYER_SET_VOLUME_CALLBACK
        )
        _FakeMediaPlayerEntity._get_state_message = _VENDOR_MEDIA_PLAYER_GET_STATE
        _FakeMediaPlayerEntity._update_state = _VENDOR_MEDIA_PLAYER_UPDATE_STATE
        _FakeServerState.persist_volume = _VENDOR_SERVER_STATE_PERSIST_VOLUME

        aioesphomeapi = ModuleType("aioesphomeapi")
        aioesphomeapi.__path__ = []  # type: ignore[attr-defined]
        api_pb2 = ModuleType("aioesphomeapi.api_pb2")
        api_pb2.VoiceAssistantRequest = _FakeRequest  # type: ignore[attr-defined]
        api_pb2.MediaPlayerCommandRequest = (  # type: ignore[attr-defined]
            _FakeMediaPlayerCommandRequest
        )
        api_model = ModuleType("aioesphomeapi.model")
        api_model.MediaPlayerCommand = _FakeMediaPlayerCommand  # type: ignore[attr-defined]
        linux_voice_assistant = ModuleType("linux_voice_assistant")
        linux_voice_assistant.__path__ = []  # type: ignore[attr-defined]
        entity_module = ModuleType("linux_voice_assistant.entity")
        entity_module.MediaPlayerEntity = _FakeMediaPlayerEntity  # type: ignore[attr-defined]
        models_module = ModuleType("linux_voice_assistant.models")
        models_module.ServerState = _FakeServerState  # type: ignore[attr-defined]
        base_satellite = ModuleType("linux_voice_assistant.satellite")
        base_satellite.VoiceSatelliteProtocol = _FakeProtocol  # type: ignore[attr-defined]
        thirdreality = ModuleType("thirdreality")
        thirdreality.__path__ = []  # type: ignore[attr-defined]
        tr_satellite = ModuleType("thirdreality.satellite")
        tr_satellite.TRSatelliteProtocol = _FakeTRProtocol  # type: ignore[attr-defined]
        tr_satellite._SOUND_CONF = _FakeSoundConfigPath()  # type: ignore[attr-defined]
        tr_satellite._VOLUME_POLL_INTERVAL = (  # type: ignore[attr-defined]
            system_volume_poll_interval
        )
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
        monkeypatch.setitem(sys.modules, "aioesphomeapi.model", api_model)
        monkeypatch.setitem(sys.modules, "linux_voice_assistant", linux_voice_assistant)
        monkeypatch.setitem(sys.modules, "linux_voice_assistant.entity", entity_module)
        monkeypatch.setitem(sys.modules, "linux_voice_assistant.models", models_module)
        monkeypatch.setitem(
            sys.modules,
            "linux_voice_assistant.satellite",
            base_satellite,
        )
        monkeypatch.setitem(sys.modules, "thirdreality", thirdreality)
        monkeypatch.setitem(sys.modules, "thirdreality.satellite", tr_satellite)
        if aec3_config is not None:
            support = ModuleType("realtime_client")
            support.ConfigError = RealtimeConfigError  # type: ignore[attr-defined]
            support.DEVICE_WEBRTC_TRANSPORT = (  # type: ignore[attr-defined]
                DEVICE_WEBRTC_TRANSPORT
            )
            support.NATIVE_AEC3_CAPTURE = NATIVE_AEC3_CAPTURE  # type: ignore[attr-defined]
            support.load_config = load_realtime_config  # type: ignore[attr-defined]
            support.normalize_wake_phrase = normalize_wake_phrase  # type: ignore[attr-defined]
            support.prewarm_device_webrtc = lambda: True  # type: ignore[attr-defined]
            support.shutdown_all_sessions = lambda: None  # type: ignore[attr-defined]
        elif realtime_support is not None:
            support = realtime_support
        else:
            support = ModuleType("realtime_client")
            support.ConfigError = RealtimeConfigError  # type: ignore[attr-defined]
            support.DEVICE_WEBRTC_TRANSPORT = (  # type: ignore[attr-defined]
                DEVICE_WEBRTC_TRANSPORT
            )
            support.NATIVE_AEC3_CAPTURE = NATIVE_AEC3_CAPTURE  # type: ignore[attr-defined]

            def missing_config() -> None:
                raise FileNotFoundError

            support.load_config = missing_config  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "realtime_client", support)

        def fake_find_spec(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "linux_voice_assistant.__main__":
                return SimpleNamespace(origin=str(vendor_main))
            return real_find_spec(name, *args, **kwargs)

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

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
        if aec3_config is None:
            spec.loader.exec_module(module)
        else:
            config_path = tmp_path / f"aec3-config-{len(loaded_modules)}.json"
            config_bytes = (
                json.dumps(aec3_config).encode()
                if isinstance(aec3_config, dict)
                else aec3_config
            )
            config_path.write_bytes(config_bytes)
            config_path.chmod(aec3_config_mode)
            production_config_path = Path("/data/conf/codex-realtime.json")
            real_open = os.open
            real_fstat = os.fstat
            config_descriptors: set[int] = set()

            def config_open(path: Any, flags: int, *args: Any) -> int:
                if path == production_config_path:
                    descriptor = real_open(config_path, flags, *args)
                    config_descriptors.add(descriptor)
                    return descriptor
                return real_open(path, flags, *args)

            def config_fstat(descriptor: int) -> Any:
                metadata = real_fstat(descriptor)
                if descriptor not in config_descriptors:
                    return metadata
                return SimpleNamespace(
                    st_mode=metadata.st_mode,
                    st_uid=aec3_config_uid,
                    st_size=metadata.st_size,
                )

            with monkeypatch.context() as config_patch:
                config_patch.setattr(os, "open", config_open)
                config_patch.setattr(os, "fstat", config_fstat)
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
        monkeypatch.setattr(module.syslog, "syslog", lambda *_args: None)
        return _FakeTRProtocol, module, tr_satellite

    yield load

    for module in reversed(loaded_modules):
        module._shutdown_led_worker()
    _FakeProtocol.wakeup = _VENDOR_BASE_WAKEUP
    _FakeProtocol._on_wakeup_sound_finished = _VENDOR_BASE_FINISH
    _FakeProtocol.handle_audio = _VENDOR_BASE_HANDLE_AUDIO
    _FakeProtocol.stop = _VENDOR_BASE_STOP
    _FakeProtocol.handle_message = _VENDOR_BASE_HANDLE_MESSAGE
    _FakeTRProtocol.wakeup = _VENDOR_TR_WAKEUP
    _FakeTRProtocol.__init__ = _VENDOR_TR_INIT
    _FakeTRProtocol.handle_message = _VENDOR_TR_HANDLE_MESSAGE
    _FakeTRProtocol._sync_volume_to_system = _VENDOR_TR_SYNC_VOLUME
    _FakeTRProtocol._sync_volume_from_system = _VENDOR_TR_SYNC_VOLUME_FROM_SYSTEM
    _FakeTRProtocol._sync_state_from_system = _VENDOR_TR_SYNC_STATE_FROM_SYSTEM
    _FakeTRProtocol._system_sync_loop = _VENDOR_TR_SYSTEM_SYNC_LOOP
    _FakeTRProtocol._update_sound_config = _VENDOR_TR_UPDATE_SOUND_CONFIG
    _FakeTRProtocol._install_volume_bridge = _VENDOR_TR_INSTALL_VOLUME_BRIDGE
    _FakeMediaPlayerEntity.__init__ = _VENDOR_MEDIA_PLAYER_INIT
    _FakeMediaPlayerEntity.handle_message = _VENDOR_MEDIA_PLAYER_HANDLE_MESSAGE
    _FakeMediaPlayerEntity._apply_volume = _VENDOR_MEDIA_PLAYER_APPLY_VOLUME
    _FakeMediaPlayerEntity.apply_volume_from_state = (
        _VENDOR_MEDIA_PLAYER_APPLY_VOLUME_FROM_STATE
    )
    _FakeMediaPlayerEntity.set_volume_callback = (
        _VENDOR_MEDIA_PLAYER_SET_VOLUME_CALLBACK
    )
    _FakeMediaPlayerEntity._get_state_message = _VENDOR_MEDIA_PLAYER_GET_STATE
    _FakeMediaPlayerEntity._update_state = _VENDOR_MEDIA_PLAYER_UPDATE_STATE
    _FakeServerState.persist_volume = _VENDOR_SERVER_STATE_PERSIST_VOLUME


def test_enabled_native_aec3_is_installed_during_overlay_import(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch = object()
    calls: list[dict[str, str]] = []
    aec3_capture = ModuleType("aec3_capture")

    def install_from_environment(*, environ: dict[str, str]) -> object:
        calls.append(environ)
        return patch

    aec3_capture.install_from_environment = (  # type: ignore[attr-defined]
        install_from_environment
    )
    monkeypatch.setenv("CODEX_AEC3_CAPTURE", "1")
    monkeypatch.setenv("CODEX_AEC3_ACTIVE", "inherited")
    monkeypatch.setitem(sys.modules, "aec3_capture", aec3_capture)
    support = _fake_realtime_support(
        media_transport=DEVICE_WEBRTC_TRANSPORT,
        capture_backend=NATIVE_AEC3_CAPTURE,
    )

    _protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)

    assert len(calls) == 1
    assert calls[0]["CODEX_AEC3_CAPTURE"] == "1"
    assert module._AEC3_CAPTURE_PATCH is patch
    assert os.environ["CODEX_AEC3_ACTIVE"] == "1"


def test_secure_config_installs_native_aec3_and_publishes_active_proof(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch = object()
    calls: list[dict[str, str]] = []
    aec3_capture = ModuleType("aec3_capture")

    def install_from_environment(*, environ: dict[str, str]) -> object:
        calls.append(environ)
        return patch

    aec3_capture.install_from_environment = (  # type: ignore[attr-defined]
        install_from_environment
    )
    monkeypatch.delenv("CODEX_AEC3_CAPTURE", raising=False)
    monkeypatch.setenv("CODEX_AEC3_ACTIVE", "inherited")
    monkeypatch.setitem(sys.modules, "aec3_capture", aec3_capture)

    _protocol, module, _tr_satellite = load_overlay(
        _REALTIME_HASHES,
        aec3_config=_native_aec3_config(),
    )

    assert len(calls) == 1
    assert calls[0]["CODEX_AEC3_CAPTURE"] == "1"
    assert module._AEC3_CAPTURE_PATCH is patch
    assert os.environ["CODEX_AEC3_ACTIVE"] == "1"


def test_bridge_pcm_native_aec3_installs_without_sidecar_prewarm(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch = object()
    aec3_capture = ModuleType("aec3_capture")
    aec3_capture.install_from_environment = (  # type: ignore[attr-defined]
        lambda *, environ: patch
    )
    monkeypatch.setitem(sys.modules, "aec3_capture", aec3_capture)
    support = _fake_realtime_support(
        media_transport="bridge_pcm",
        capture_backend=NATIVE_AEC3_CAPTURE,
        full_duplex=True,
        prewarm_result=False,
    )

    _protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)

    assert module._AEC3_CAPTURE_PATCH is patch
    assert module._REALTIME_CONFIG.capture_backend == NATIVE_AEC3_CAPTURE
    assert support.prewarm_calls == []  # type: ignore[attr-defined]
    assert os.environ["CODEX_AEC3_ACTIVE"] == "1"


def test_environment_override_promotes_valid_device_config_to_native_aec3(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch = object()
    aec3_capture = ModuleType("aec3_capture")
    aec3_capture.install_from_environment = (  # type: ignore[attr-defined]
        lambda *, environ: patch
    )
    monkeypatch.setenv("CODEX_AEC3_CAPTURE", "true")
    monkeypatch.setitem(sys.modules, "aec3_capture", aec3_capture)

    _protocol, module, _tr_satellite = load_overlay(
        _REALTIME_HASHES,
        aec3_config=_native_aec3_config(capture_backend="pulseaudio_aec"),
    )

    assert module._AEC3_CAPTURE_PATCH is patch
    assert module._REALTIME_CONFIG.capture_backend == NATIVE_AEC3_CAPTURE
    assert os.environ["CODEX_AEC3_ACTIVE"] == "1"


def test_invalid_aec3_environment_fails_closed_without_config(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_AEC3_CAPTURE", "tru")
    monkeypatch.setenv("CODEX_AEC3_ACTIVE", "inherited")

    with pytest.raises(SystemExit, match="configuration is invalid"):
        load_overlay(_REALTIME_HASHES)

    assert "CODEX_AEC3_ACTIVE" not in os.environ


def test_enabled_aec3_override_requires_secure_realtime_config(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_AEC3_CAPTURE", "1")

    with pytest.raises(SystemExit, match="requires a valid enabled realtime"):
        load_overlay(_REALTIME_HASHES)

    assert "CODEX_AEC3_ACTIVE" not in os.environ


@pytest.mark.parametrize("installer_result", [None, RuntimeError("broken ABI")])
def test_selected_native_aec3_installer_failure_is_process_fatal(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
    installer_result: object,
) -> None:
    aec3_capture = ModuleType("aec3_capture")

    def install_from_environment(*, environ: dict[str, str]) -> object:
        del environ
        if isinstance(installer_result, Exception):
            raise installer_result
        return installer_result

    aec3_capture.install_from_environment = (  # type: ignore[attr-defined]
        install_from_environment
    )
    monkeypatch.setitem(sys.modules, "aec3_capture", aec3_capture)

    with pytest.raises(SystemExit, match="native AEC3 capture"):
        load_overlay(
            _REALTIME_HASHES,
            aec3_config=_native_aec3_config(),
        )

    assert "CODEX_AEC3_ACTIVE" not in os.environ


def test_selected_native_aec3_requires_matching_guarded_realtime_overlay(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aec3_capture = ModuleType("aec3_capture")
    aec3_capture.install_from_environment = (  # type: ignore[attr-defined]
        lambda *, environ: object()
    )
    support = _fake_realtime_support(
        media_transport=DEVICE_WEBRTC_TRANSPORT,
        capture_backend=NATIVE_AEC3_CAPTURE,
    )
    hashes = list(_REALTIME_HASHES)
    hashes[4] = "unknown"
    monkeypatch.setitem(sys.modules, "aec3_capture", aec3_capture)

    with pytest.raises(SystemExit, match="guarded realtime overlay"):
        load_overlay(tuple(hashes), support)

    assert "CODEX_AEC3_ACTIVE" not in os.environ


def test_selected_native_aec3_requires_successful_direct_prewarm(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aec3_capture = ModuleType("aec3_capture")
    aec3_capture.install_from_environment = (  # type: ignore[attr-defined]
        lambda *, environ: object()
    )
    support = _fake_realtime_support(
        media_transport=DEVICE_WEBRTC_TRANSPORT,
        capture_backend=NATIVE_AEC3_CAPTURE,
        prewarm_result=False,
    )
    monkeypatch.setitem(sys.modules, "aec3_capture", aec3_capture)

    with pytest.raises(SystemExit, match="requires direct WebRTC prewarm"):
        load_overlay(_REALTIME_HASHES, support)

    assert "CODEX_AEC3_ACTIVE" not in os.environ


@pytest.mark.parametrize(
    "config",
    [
        _native_aec3_config(enabled=False),
        _native_aec3_config(capture_backend="pulseaudio_aec"),
    ],
)
def test_native_aec3_config_selection_ignores_unselected_config(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, object],
) -> None:
    monkeypatch.delenv("CODEX_AEC3_CAPTURE", raising=False)
    monkeypatch.setenv("CODEX_AEC3_ACTIVE", "inherited")

    _protocol, module, _tr_satellite = load_overlay(
        _REALTIME_HASHES,
        aec3_config=config,
    )

    assert module._AEC3_CAPTURE_PATCH is None
    assert "CODEX_AEC3_ACTIVE" not in os.environ


@pytest.mark.parametrize(
    ("config", "mode", "owner_uid"),
    [
        (_native_aec3_config(unknown=True), 0o600, 0),
        (_native_aec3_config(full_duplex=False), 0o600, 0),
        (_native_aec3_config(), 0o640, 0),
        (_native_aec3_config(), 0o600, 1_000),
    ],
)
def test_native_aec3_selection_rejects_invalid_or_untrusted_config(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, object],
    mode: int,
    owner_uid: int,
) -> None:
    monkeypatch.delenv("CODEX_AEC3_CAPTURE", raising=False)
    monkeypatch.setenv("CODEX_AEC3_ACTIVE", "inherited")

    _protocol, module, _tr_satellite = load_overlay(
        _REALTIME_HASHES,
        aec3_config=config,
        aec3_config_mode=mode,
        aec3_config_uid=owner_uid,
    )

    assert module._AEC3_CAPTURE_PATCH is None
    assert module._REALTIME_CONFIG is None
    assert "CODEX_AEC3_ACTIVE" not in os.environ


def _wake(instance: _FakeProtocol, phrase: str = "okay nabu") -> None:
    instance.wakeup(SimpleNamespace(wake_word=phrase))


def _pcm_frame(marker: int, *, samples: int = 4) -> bytes:
    """Return one distinct, aligned PCM16 test frame."""
    return bytes((marker, 0)) * samples


def _mark_direct_ready(instance: Any, session: Any) -> None:
    """Publish one valid ready boundary and wait for the startup watcher."""
    session.ready_at = time.monotonic()
    session.ready = True
    session.state = _FakeSessionState.READY
    for _ in range(100):
        if instance.state.tts_player.callbacks:
            return
        time.sleep(0.002)
    raise AssertionError("direct ready cue did not start")


def _open_direct_capture_for_volume_test(instance: Any) -> None:
    """Place a direct owner at the post-cue volume-monitor boundary."""
    owner = instance._codex_realtime_owner
    assert owner is not None
    owner.capture_open = True


def _volume_command(volume: float, *, key: int = 17) -> _FakeMediaPlayerCommandRequest:
    return _FakeMediaPlayerCommandRequest(
        key=key,
        has_volume=True,
        volume=volume,
    )


def _player_command(command: _FakeMediaPlayerCommand) -> _FakeMediaPlayerCommandRequest:
    return _FakeMediaPlayerCommandRequest(
        has_command=True,
        command=command,
    )


def test_opt_in_voice_sample_ring_is_bounded_and_detached_without_joining(
    load_overlay: Any,
) -> None:
    _protocol, module, _tr_satellite = load_overlay(
        _REALTIME_HASHES,
        _fake_realtime_support(),
    )
    module._WAKE_SAMPLE_UPLOADER = object()
    instance = SimpleNamespace()
    chunks = [bytes((index, 0)) * 1_024 for index in range(50)]

    for chunk in chunks:
        module._remember_voice_sample_preroll(instance, chunk)
    retained = module._take_voice_sample_preroll(instance)

    assert retained == chunks[-48:]
    assert sum(map(len, retained)) == 96 * 1_024
    assert module._take_voice_sample_preroll(instance) == []


def _fake_realtime_support(
    *,
    fallback_buffer_bytes: int = 64 * 1024,
    input_queue_bytes: int = 64 * 1024,
    constructor_error: Exception | None = None,
    start_error: Exception | None = None,
    start_entered: threading.Event | None = None,
    start_release: threading.Event | None = None,
    submit_entered: threading.Event | None = None,
    submit_release: threading.Event | None = None,
    media_transport: str = "bridge_pcm",
    capture_backend: str = "pulseaudio_aec",
    full_duplex: bool | None = None,
    prewarm_result: bool = True,
    wake_probability_cutoff: float | None = 0.85,
    personalized_wake_config_path: str | None = None,
    wake_phrase: str = "okay computer",
    realtime_only: bool = False,
    aec_sink_volume_ceiling_percent: int = 60,
    playback_volume_percent: int = 60,
) -> ModuleType:
    support = ModuleType("realtime_client")
    config = SimpleNamespace(
        wake_phrase=wake_phrase,
        fallback_buffer_bytes=fallback_buffer_bytes,
        input_queue_bytes=input_queue_bytes,
        media_transport=media_transport,
        capture_backend=capture_backend,
        full_duplex=(media_transport == "device_webrtc")
        if full_duplex is None
        else full_duplex,
        wake_probability_cutoff=wake_probability_cutoff,
        personalized_wake_config_path=personalized_wake_config_path,
        realtime_only=realtime_only,
        aec_sink_volume_ceiling_percent=aec_sink_volume_ceiling_percent,
        playback_volume_percent=playback_volume_percent,
    )
    sessions: list[Any] = []

    class ConfigError(ValueError):
        pass

    class SubmitResult:
        ACCEPTED = object()
        GATED = object()
        FULL = object()
        CLOSED = object()
        INVALID = object()

    class RealtimeSession:
        def __init__(self, received_config: object) -> None:
            assert received_config is config
            if constructor_error is not None:
                raise constructor_error
            self.ready = False
            self.ready_at = None
            self.failed_before_ready = False
            self.terminal = False
            self.terminal_reason = None
            self.state = _FakeSessionState.NEW
            self.submit_result = SubmitResult.ACCEPTED
            self.started = 0
            self.stopped = 0
            self.interrupted = 0
            self.interrupt_preserve_session: list[bool] = []
            self.live_capture_opened = 0
            self.audio: list[bytes] = []
            self.volume_requests: list[int] = []
            self.volume_request_states: list[_FakeSessionState] = []
            self.reconcile_requests: list[int] = []
            self.reconcile_request_states: list[_FakeSessionState] = []
            sessions.append(self)

        def start(self) -> None:
            self.started += 1
            self.state = _FakeSessionState.CONNECTING
            if start_entered is not None:
                start_entered.set()
            if start_release is not None and not start_release.wait(2):
                raise RuntimeError("test start barrier timed out")
            if start_error is not None:
                raise start_error

        def stop(self) -> None:
            self.stopped += 1
            self.state = _FakeSessionState.STOPPING

        def interrupt(self, *, preserve_session: bool = True) -> None:
            self.interrupted += 1
            self.interrupt_preserve_session.append(preserve_session)
            self.state = _FakeSessionState.STOPPING

        def submit_audio(self, value: bytes) -> object:
            if len(value) % 2:
                return SubmitResult.INVALID
            if submit_entered is not None:
                submit_entered.set()
            if submit_release is not None and not submit_release.wait(2):
                raise RuntimeError("test submit barrier timed out")
            self.audio.append(value)
            return self.submit_result

        def notify_live_capture_opened(self) -> None:
            self.live_capture_opened += 1

        def request_playback_volume(self, percent: int) -> int:
            self.volume_requests.append(percent)
            self.volume_request_states.append(self.state)
            return min(
                percent,
                config.aec_sink_volume_ceiling_percent,
                config.playback_volume_percent,
            )

        def reconcile_playback_volume(self, percent: int) -> int:
            self.reconcile_requests.append(percent)
            self.reconcile_request_states.append(self.state)
            return min(
                percent,
                config.aec_sink_volume_ceiling_percent,
                config.playback_volume_percent,
            )

    support.ConfigError = ConfigError  # type: ignore[attr-defined]
    support.DEVICE_WEBRTC_TRANSPORT = "device_webrtc"  # type: ignore[attr-defined]
    support.NATIVE_AEC3_CAPTURE = "native_aec3"  # type: ignore[attr-defined]
    support.SubmitResult = SubmitResult  # type: ignore[attr-defined]
    support.RealtimeSession = RealtimeSession  # type: ignore[attr-defined]
    support.load_config = lambda: config  # type: ignore[attr-defined]
    support.normalize_wake_phrase = (  # type: ignore[attr-defined]
        lambda phrase: " ".join(phrase.casefold().split())
    )
    support.shutdown_all_sessions = lambda: None  # type: ignore[attr-defined]
    prewarm_calls: list[None] = []

    def prewarm_device_webrtc() -> bool:
        prewarm_calls.append(None)
        return prewarm_result

    support.prewarm_device_webrtc = prewarm_device_webrtc  # type: ignore[attr-defined]
    bridge_prewarm_calls: list[object] = []
    scheduled_bridge_prewarm_calls: list[object] = []

    def prewarm_bridge_pcm(received_config: object) -> bool:
        assert received_config is config
        bridge_prewarm_calls.append(received_config)
        return len(bridge_prewarm_calls) == 1

    def schedule_bridge_pcm_prewarm(received_config: object) -> bool:
        assert received_config is config
        scheduled_bridge_prewarm_calls.append(received_config)
        return True

    support.prewarm_bridge_pcm = prewarm_bridge_pcm  # type: ignore[attr-defined]
    support.schedule_bridge_pcm_prewarm = (  # type: ignore[attr-defined]
        schedule_bridge_pcm_prewarm
    )
    support.take_prewarmed_bridge_pcm = lambda _config: None  # type: ignore[attr-defined]
    support.prewarm_calls = prewarm_calls  # type: ignore[attr-defined]
    support.bridge_prewarm_calls = bridge_prewarm_calls  # type: ignore[attr-defined]
    support.scheduled_bridge_prewarm_calls = (  # type: ignore[attr-defined]
        scheduled_bridge_prewarm_calls
    )
    support.sessions = sessions  # type: ignore[attr-defined]
    return support


@pytest.mark.parametrize(
    ("media_transport", "expected_calls"),
    [("device_webrtc", [None]), ("bridge_pcm", [])],
)
def test_only_direct_webrtc_prewarms_after_guarded_overlay_activation(
    load_overlay: Any,
    media_transport: str,
    expected_calls: list[None],
) -> None:
    support = _fake_realtime_support(
        media_transport=media_transport,
        full_duplex=True,
    )

    _protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)

    assert module._REALTIME_PATCH_ACTIVE
    assert support.prewarm_calls == expected_calls  # type: ignore[attr-defined]


def test_bridge_pcm_probable_wake_score_starts_nonblocking_prewarm(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(
        media_transport="bridge_pcm",
        full_duplex=True,
        wake_phrase="okay nabu",
        realtime_only=True,
    )
    protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    detector = instance.state.wake_words["okay_nabu"]

    detector._probabilities[:] = [module._BRIDGE_PCM_PREWAKE_PROBABILITY - 0.01]
    instance.handle_audio(_pcm_frame(1))
    assert support.bridge_prewarm_calls == []  # type: ignore[attr-defined]

    detector._probabilities[:] = [module._BRIDGE_PCM_PREWAKE_PROBABILITY]
    instance.handle_audio(_pcm_frame(2))
    assert support.bridge_prewarm_calls == [support.load_config()]  # type: ignore[attr-defined]


def test_bridge_pcm_wake_claims_connecting_prewarm_without_second_start(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(
        media_transport="bridge_pcm",
        full_duplex=True,
        wake_phrase="okay nabu",
        realtime_only=True,
    )
    prewarmed = support.RealtimeSession(support.load_config())  # type: ignore[attr-defined]
    prewarmed.start()
    support.take_prewarmed_bridge_pcm = (  # type: ignore[attr-defined]
        lambda _config: prewarmed
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()

    _wake(instance, "okay nabu")

    assert instance._codex_realtime_owner.session is prewarmed
    assert prewarmed.started == 1
    instance.stop()


def test_guarded_direct_overlay_uses_50ms_system_volume_poll(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, module, tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    observed_delays: list[float] = []

    async def stop_after_one_poll(delay: float) -> None:
        observed_delays.append(delay)
        instance.state.connected = False

    monkeypatch.setattr(asyncio, "sleep", stop_after_one_poll)

    asyncio.run(instance._system_sync_loop())

    assert module._REALTIME_PATCH_ACTIVE
    assert tr_satellite._VOLUME_POLL_INTERVAL == 0.05  # type: ignore[attr-defined]
    assert observed_delays == [0.05]


def test_unrecognized_vendor_poll_constant_fails_closed_atomically(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")

    _protocol, module, tr_satellite = load_overlay(
        _REALTIME_HASHES,
        support,
        system_volume_poll_interval=0.25,
    )

    assert module._REALTIME_PATCH_ACTIVE is False
    assert tr_satellite._VOLUME_POLL_INTERVAL == 0.25  # type: ignore[attr-defined]
    assert _FakeProtocol.handle_message is _VENDOR_BASE_HANDLE_MESSAGE
    assert _FakeTRProtocol._sync_volume_from_system is (
        _VENDOR_TR_SYNC_VOLUME_FROM_SYSTEM
    )


def test_direct_session_starts_at_current_software_volume_without_player_mutation(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(
        media_transport="device_webrtc",
        aec_sink_volume_ceiling_percent=60,
        playback_volume_percent=50,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    entity = instance.state.media_player_entity
    entity.volume = 0.35
    entity.previous_volume = 0.35

    _wake(instance, "okay computer")

    session = support.sessions[0]  # type: ignore[attr-defined]
    assert session.volume_requests == [35]
    assert session.volume_request_states == [_FakeSessionState.NEW]
    assert session.state is _FakeSessionState.CONNECTING
    assert entity.volume == 0.35
    assert entity.previous_volume == 0.35
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []
    assert entity.server.state.persisted_volumes == []


def test_direct_session_clamps_stale_current_volume_before_start_and_persists(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(
        media_transport="device_webrtc",
        aec_sink_volume_ceiling_percent=55,
        playback_volume_percent=40,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    entity = instance.state.media_player_entity
    entity.volume = 0.9
    entity.previous_volume = 0.9

    _wake(instance, "okay computer")

    session = support.sessions[0]  # type: ignore[attr-defined]
    assert session.volume_requests == [40]
    assert entity.volume == 0.4
    assert entity.previous_volume == 0.4
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []
    assert entity.server.state.persisted_volumes == [0.4]


def test_live_direct_volume_is_clamped_in_software_before_state_and_persistence(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _fake_realtime_support(
        media_transport="device_webrtc",
        aec_sink_volume_ceiling_percent=60,
        playback_volume_percent=50,
    )
    protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    entity = instance.state.media_player_entity
    entity.volume = 0.4
    entity.previous_volume = 0.4
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    _open_direct_capture_for_volume_test(instance)
    instance.events.clear()
    session.volume_requests.clear()
    diagnostics: list[str] = []
    monkeypatch.setattr(
        module.syslog,
        "syslog",
        lambda _priority, message: diagnostics.append(message),
    )

    def request_volume(percent: int) -> int:
        instance.events.append(f"request:{percent}")
        session.volume_requests.append(percent)
        return percent

    session.request_playback_volume = request_volume
    responses = list(instance.handle_message(_volume_command(1.0)))

    assert session.volume_requests == [50]
    assert entity.volume == 0.5
    assert entity.previous_volume == 0.5
    assert entity.server.state.persisted_volumes == [0.5]
    assert instance.sound_config["volume"] == 50
    assert instance.sound_config_updates == [{"volume": 50}]
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []
    assert instance.events == ["request:50", "persist:0.50", "state"]
    expected_diagnostic = (
        "codex-voice realtime_volume source=command "
        "requested_percent=100 applied_percent=50"
    )
    assert diagnostics == [expected_diagnostic]
    assert responses == [("media-state", "idle", 0.5, False)]

    session.reconcile_requests.clear()
    entity.server.state.persisted_volumes.clear()
    instance.sound_config_updates.clear()
    instance._sync_volume_from_system()

    assert session.reconcile_requests == [50]
    assert entity.server.state.persisted_volumes == []
    assert instance.sound_config_updates == []
    assert instance._codex_realtime_anchor_dirty is False


def test_live_direct_volume_below_anchor_is_applied_without_clamping(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(
        media_transport="device_webrtc",
        aec_sink_volume_ceiling_percent=60,
        playback_volume_percent=50,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    instance.state.media_player_entity.volume = 0.5
    instance.state.media_player_entity.previous_volume = 0.5
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    session.volume_requests.clear()

    list(instance.handle_message(_volume_command(0.37)))

    entity = instance.state.media_player_entity
    assert session.volume_requests == [37]
    assert entity.volume == 0.37
    assert entity.previous_volume == 0.37
    assert entity.server.state.persisted_volumes == [0.37]
    assert instance.sound_config["volume"] == 37
    assert instance.sound_config_updates == [{"volume": 37}]
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []


@pytest.mark.parametrize(
    (
        "physical_volume",
        "muted",
        "expected_request",
        "expected_entity_volume",
        "expected_previous_volume",
        "expected_logical_volume",
    ),
    [
        (0.3, False, 30, 0.3, 0.3, 0.3),
        (0.5, False, 50, 0.5, 0.5, 0.5),
        (0.8, False, 60, 0.6, 0.6, 0.6),
        (0.3, True, 0, 0.0, 0.3, 0.3),
    ],
)
def test_physical_volume_reconciles_anchor_without_mpv_setters(
    load_overlay: Any,
    physical_volume: float,
    muted: bool,
    expected_request: int,
    expected_entity_volume: float,
    expected_previous_volume: float,
    expected_logical_volume: float,
) -> None:
    support = _fake_realtime_support(
        media_transport="device_webrtc",
        aec_sink_volume_ceiling_percent=60,
        playback_volume_percent=60,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    _open_direct_capture_for_volume_test(instance)
    entity = instance.state.media_player_entity
    if muted:
        entity.muted = True
        entity.volume = 0.0
        entity.previous_volume = 0.6
    session.reconcile_requests.clear()
    instance.state.persisted_volumes.clear()
    instance.sent_messages.clear()
    instance.events.clear()
    instance.write_physical_volume(round(physical_volume * 100))

    instance._sync_volume_from_system()

    assert session.reconcile_requests == [expected_request]
    assert entity.volume == expected_entity_volume
    assert entity.previous_volume == expected_previous_volume
    assert entity.muted is muted
    assert instance.state.volume == expected_logical_volume
    assert instance.state.persisted_volumes == [expected_logical_volume]
    assert instance._last_system_volume == expected_logical_volume
    assert instance.sound_config["volume"] == round(expected_logical_volume * 100)
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []
    assert instance.sound_config_updates == [
        {"volume": round(expected_logical_volume * 100)}
    ]
    assert instance.sent_messages == [entity._get_state_message()]


def test_physical_volume_above_ceiling_is_rewritten_then_next_poll_is_noop(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(
        media_transport="device_webrtc",
        aec_sink_volume_ceiling_percent=60,
        playback_volume_percent=60,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    _open_direct_capture_for_volume_test(instance)
    entity = instance.state.media_player_entity
    session.reconcile_requests.clear()
    instance.state.persisted_volumes.clear()
    instance.sound_config_updates.clear()
    instance.write_physical_volume(80)

    instance._sync_volume_from_system()

    assert session.reconcile_requests == [60]
    assert entity.volume == 0.6
    assert instance.state.volume == 0.6
    assert instance.state.persisted_volumes == [0.6]
    assert instance.sound_config_updates == [{"volume": 60}]
    assert instance.sound_config["volume"] == 60
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []

    session.reconcile_requests.clear()
    instance.state.persisted_volumes.clear()
    instance.sound_config_updates.clear()
    instance._sync_volume_from_system()

    assert session.reconcile_requests == [60]
    assert instance.state.persisted_volumes == []
    assert instance.sound_config_updates == []
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []

    session.reconcile_requests.clear()
    instance._sync_volume_from_system()

    assert session.reconcile_requests == []


def test_repeated_physical_down_uses_normalized_ten_percent_steps(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    _open_direct_capture_for_volume_test(instance)
    entity = instance.state.media_player_entity
    session.reconcile_requests.clear()
    instance.state.persisted_volumes.clear()
    instance.sound_config_updates.clear()

    instance.write_physical_volume(50)
    instance._sync_volume_from_system()
    instance.write_physical_volume(40)
    instance._sync_volume_from_system()

    assert session.reconcile_requests == [50, 40]
    assert instance.state.persisted_volumes == [0.5, 0.4]
    assert instance.sound_config_updates == [{"volume": 50}, {"volume": 40}]
    assert entity.volume == 0.4
    assert entity.previous_volume == 0.4
    assert instance.state.volume == 0.4
    assert instance.sound_config["volume"] == 40
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []


@pytest.mark.parametrize("failure_mode", ["rejected", "exception"])
def test_sound_config_update_failure_fences_active_owner(
    load_overlay: Any,
    failure_mode: str,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    _open_direct_capture_for_volume_test(instance)
    entity = instance.state.media_player_entity
    session.reconcile_requests.clear()
    instance.state.persisted_volumes.clear()
    instance.write_physical_volume(30)
    instance.fail_sound_config_update = failure_mode == "rejected"
    instance.raise_sound_config_update = failure_mode == "exception"

    instance._sync_volume_from_system()

    assert session.reconcile_requests == [30]
    assert session.interrupted == 1
    assert session.interrupt_preserve_session == [False]
    assert session.state is _FakeSessionState.STOPPING
    assert instance._codex_realtime_owner is None
    assert instance.state.persisted_volumes == [0.3]
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []


def test_direct_sound_config_writer_shares_lock_and_replaces_atomically(
    load_overlay: Any,
    tmp_path: Path,
) -> None:
    _protocol, module, tr_satellite = load_overlay(_REALTIME_HASHES)
    sound_path = tmp_path / "sound.json"
    lock_path = tmp_path / "sound_config.lock"
    sound_path.write_text(
        json.dumps({"volume": 60, "mic_gain": 30, "mic_mute": 0}),
        encoding="utf-8",
    )
    sound_path.chmod(0o600)
    original_inode = sound_path.stat().st_ino
    tr_satellite._SOUND_CONF = sound_path  # type: ignore[attr-defined]
    module._SOUND_CONFIG_LOCK_PATH = str(lock_path)

    assert module._atomic_update_direct_sound_config({"volume": 30}) is True

    assert json.loads(sound_path.read_text(encoding="utf-8")) == {
        "volume": 30,
        "mic_gain": 30,
        "mic_mute": 0,
    }
    assert sound_path.stat().st_ino != original_inode
    assert sound_path.stat().st_mode & 0o777 == 0o600


def test_direct_sound_config_writer_times_out_without_unlocked_fallback(
    load_overlay: Any,
    tmp_path: Path,
) -> None:
    _protocol, module, tr_satellite = load_overlay(_REALTIME_HASHES)
    sound_path = tmp_path / "sound.json"
    lock_path = tmp_path / "sound_config.lock"
    original = {"volume": 60, "mic_gain": 30}
    sound_path.write_text(json.dumps(original), encoding="utf-8")
    tr_satellite._SOUND_CONF = sound_path  # type: ignore[attr-defined]
    module._SOUND_CONFIG_LOCK_PATH = str(lock_path)
    module._SOUND_CONFIG_LOCK_TIMEOUT_SECONDS = 0.01
    module._SOUND_CONFIG_LOCK_RETRY_SECONDS = 0.001
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert module._atomic_update_direct_sound_config({"volume": 30}) is False
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert json.loads(sound_path.read_text(encoding="utf-8")) == original


def test_direct_sound_config_writer_rereads_after_physical_key_transaction(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _protocol, module, tr_satellite = load_overlay(_REALTIME_HASHES)
    sound_path = tmp_path / "sound.json"
    lock_path = tmp_path / "sound_config.lock"
    sound_path.write_text(
        json.dumps({"volume": 60, "mic_mute": 0}),
        encoding="utf-8",
    )
    tr_satellite._SOUND_CONF = sound_path  # type: ignore[attr-defined]
    module._SOUND_CONFIG_LOCK_PATH = str(lock_path)
    attempted = threading.Event()
    real_flock = fcntl.flock

    def observed_flock(descriptor: int, operation: int) -> Any:
        if operation & fcntl.LOCK_NB:
            attempted.set()
        return real_flock(descriptor, operation)

    monkeypatch.setattr(
        module,
        "fcntl",
        SimpleNamespace(
            flock=observed_flock,
            LOCK_EX=fcntl.LOCK_EX,
            LOCK_NB=fcntl.LOCK_NB,
            LOCK_UN=fcntl.LOCK_UN,
        ),
    )
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    real_flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    results: list[bool | None] = []
    writer = threading.Thread(
        target=lambda: results.append(
            module._atomic_update_direct_sound_config({"volume": 30})
        ),
        daemon=True,
    )
    writer.start()
    assert attempted.wait(1.0)
    physical_temp = tmp_path / "physical.json"
    physical_temp.write_text(
        json.dumps({"volume": 50, "mic_mute": 1}),
        encoding="utf-8",
    )
    physical_temp.replace(sound_path)
    real_flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
    writer.join(1.0)

    assert not writer.is_alive()
    assert results == [True]
    assert json.loads(sound_path.read_text(encoding="utf-8")) == {
        "volume": 30,
        "mic_mute": 1,
    }


def test_unchanged_physical_volume_poll_avoids_session_and_mpv_work(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    _open_direct_capture_for_volume_test(instance)
    entity = instance.state.media_player_entity
    instance._last_system_volume = 0.6
    instance.state.volume = 0.6
    instance.sound_config["volume"] = 60
    session.reconcile_requests.clear()
    instance.state.persisted_volumes.clear()
    instance.sent_messages.clear()

    instance._sync_volume_from_system()

    assert tr_satellite._VOLUME_POLL_INTERVAL == 0.05  # type: ignore[attr-defined]
    assert session.reconcile_requests == []
    assert instance.state.persisted_volumes == []
    assert instance.sent_messages == []
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []


def test_startup_volume_poll_defers_change_until_capture_opens(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    owner = instance._codex_realtime_owner
    session.reconcile_requests.clear()
    instance.state.persisted_volumes.clear()
    instance.write_physical_volume(30)

    instance._sync_volume_from_system(force=True)

    assert owner is instance._codex_realtime_owner
    assert not owner.capture_open
    assert session.reconcile_requests == []
    assert session.interrupted == 0
    assert instance.state.persisted_volumes == []
    assert instance.sound_config["volume"] == 30

    _open_direct_capture_for_volume_test(instance)
    instance._sync_volume_from_system()

    assert session.reconcile_requests == [30]
    assert session.interrupted == 0
    assert instance.state.persisted_volumes == [0.3]
    assert instance.state.media_player_entity.volume == 0.3


def test_unreadable_physical_volume_repairs_anchor_without_changing_bookkeeping(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    _open_direct_capture_for_volume_test(instance)
    entity = instance.state.media_player_entity
    session.reconcile_requests.clear()
    instance.state.persisted_volumes.clear()
    instance.sent_messages.clear()
    instance.sound_config["volume"] = None
    sys.modules["thirdreality.satellite"]._SOUND_CONF.mark_write()  # type: ignore[attr-defined]

    instance._sync_volume_from_system()

    assert session.reconcile_requests == [60]
    assert session.interrupted == 1
    assert session.interrupt_preserve_session == [False]
    assert session.state is _FakeSessionState.STOPPING
    assert instance._codex_realtime_owner is None
    assert entity.volume == 0.6
    assert entity.previous_volume == 0.6
    assert instance.state.volume == 0.6
    assert instance.state.persisted_volumes == []
    assert instance.sent_messages == []
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []


@pytest.mark.parametrize("muted", [False, True])
def test_live_ha_volume_persistence_failure_fences_without_vendor_fallback(
    load_overlay: Any,
    muted: bool,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    entity = instance.state.media_player_entity
    if muted:
        entity.muted = True
        entity.volume = 0.0
        entity.previous_volume = 0.6
    session.volume_requests.clear()
    instance.state.persisted_volumes.clear()
    instance.fail_sound_config_update = True

    responses = list(instance.handle_message(_volume_command(0.3)))

    assert session.volume_requests == [0 if muted else 30]
    assert session.interrupted == 1
    assert session.interrupt_preserve_session == [False]
    assert session.state is _FakeSessionState.STOPPING
    assert instance._codex_realtime_owner is None
    assert instance.state.persisted_volumes == [0.3]
    assert entity.volume == (0.0 if muted else 0.3)
    assert entity.previous_volume == 0.3
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []
    assert responses == [("media-state", "idle", 0.0 if muted else 0.3, muted)]


def test_accepted_physical_reconciliation_never_also_runs_vendor_after_stop(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    _open_direct_capture_for_volume_test(instance)
    entity = instance.state.media_player_entity
    session.reconcile_requests.clear()
    instance.state.persisted_volumes.clear()
    instance.write_physical_volume(30)

    def accept_then_stop(percent: int) -> int:
        session.reconcile_requests.append(percent)
        session.terminal = True
        return percent

    session.reconcile_playback_volume = accept_then_stop

    instance._sync_volume_from_system()

    assert session.reconcile_requests == [30]
    assert entity.volume == 0.3
    assert entity.previous_volume == 0.3
    assert instance.state.persisted_volumes == [0.3]
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []


def test_physical_owner_loss_before_session_call_falls_back_exactly_once(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    _open_direct_capture_for_volume_test(instance)
    entity = instance.state.media_player_entity
    session.reconcile_requests.clear()
    instance.state.persisted_volumes.clear()
    instance.write_physical_volume(30)
    ownership_checks = 0

    def lose_before_call(
        _instance: Any,
        _owner: Any,
        *,
        allow_new: bool = False,
    ) -> bool:
        nonlocal ownership_checks
        del allow_new
        ownership_checks += 1
        return ownership_checks == 1

    monkeypatch.setattr(module, "_direct_volume_owner_is_current", lose_before_call)

    instance._sync_volume_from_system()

    assert ownership_checks == 2
    assert session.reconcile_requests == []
    assert entity.volume == 0.3
    assert entity.previous_volume == 0.3
    assert entity.music_player.volume_calls == [30]
    assert entity.announce_player.volume_calls == [30]
    assert instance.state.persisted_volumes == []


@pytest.mark.parametrize(
    "teardown_signal",
    ["terminal", "stop_requested", "stopping"],
)
def test_accepted_direct_volume_never_also_runs_vendor_after_stop_race(
    load_overlay: Any,
    teardown_signal: str,
) -> None:
    support = _fake_realtime_support(
        media_transport="device_webrtc",
        aec_sink_volume_ceiling_percent=60,
        playback_volume_percent=60,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    owner = instance._codex_realtime_owner
    session = support.sessions[0]  # type: ignore[attr-defined]
    session.volume_requests.clear()

    def request_volume(percent: int) -> int:
        session.volume_requests.append(percent)
        if teardown_signal == "terminal":
            session.terminal = True
        elif teardown_signal == "stop_requested":
            owner.stop_requested = True
        else:
            session.state = _FakeSessionState.STOPPING
        return percent

    session.request_playback_volume = request_volume
    responses = list(instance.handle_message(_volume_command(0.8)))

    entity = instance.state.media_player_entity
    assert session.volume_requests == [60]
    assert entity.volume == 0.6
    assert entity.previous_volume == 0.6
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []
    assert entity.server.state.persisted_volumes == [0.6]
    assert responses == [("media-state", "idle", 0.6, False)]


@pytest.mark.parametrize(
    "lifecycle",
    [
        _FakeSessionState.NEW,
        _FakeSessionState.STOPPING,
        _FakeSessionState.STOPPED,
        _FakeSessionState.FAILED,
    ],
)
def test_direct_volume_non_live_lifecycle_uses_vendor_before_request(
    load_overlay: Any,
    lifecycle: _FakeSessionState,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    session.volume_requests.clear()
    session.state = lifecycle

    responses = list(instance.handle_message(_volume_command(0.8)))

    entity = instance.state.media_player_entity
    assert session.volume_requests == []
    assert entity.volume == 0.8
    assert entity.previous_volume == 0.8
    assert entity.music_player.volume_calls == [80]
    assert entity.announce_player.volume_calls == [80]
    assert entity.server.state.persisted_volumes == [0.8]
    assert responses == [("media-state", "idle", 0.8, False)]


@pytest.mark.parametrize(
    "lifecycle",
    [
        _FakeSessionState.CONNECTING,
        _FakeSessionState.READY,
        _FakeSessionState.INTERRUPTING,
    ],
)
def test_direct_volume_live_lifecycle_remains_software_owned(
    load_overlay: Any,
    lifecycle: _FakeSessionState,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    session.volume_requests.clear()
    session.state = lifecycle

    responses = list(instance.handle_message(_volume_command(0.4)))

    entity = instance.state.media_player_entity
    assert session.volume_requests == [40]
    assert entity.volume == 0.4
    assert entity.previous_volume == 0.4
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []
    assert entity.server.state.persisted_volumes == [0.4]
    assert responses == [("media-state", "idle", 0.4, False)]


def test_direct_volume_live_request_failure_remains_software_fail_closed(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    session.volume_requests.clear()

    def fail_volume(percent: int) -> int:
        session.volume_requests.append(percent)
        raise RuntimeError("renderer unavailable")

    session.request_playback_volume = fail_volume
    responses = list(instance.handle_message(_volume_command(0.8)))

    entity = instance.state.media_player_entity
    assert session.volume_requests == [60]
    assert entity.volume == 0.6
    assert entity.previous_volume == 0.6
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []
    assert entity.server.state.persisted_volumes == []
    assert session.interrupted == 1
    assert session.interrupt_preserve_session == [False]
    assert session.state is _FakeSessionState.STOPPING
    assert instance._codex_realtime_owner is None
    assert responses == [("media-state", "idle", 0.6, False)]


@pytest.mark.parametrize(
    ("command", "initial_muted", "expected_request", "expected_response"),
    [
        (
            _FakeMediaPlayerCommand.MUTE,
            False,
            0,
            ("media-state", "idle", 0.6, False),
        ),
        (
            _FakeMediaPlayerCommand.UNMUTE,
            True,
            40,
            ("media-state", "idle", 0.0, True),
        ),
    ],
)
def test_live_mute_or_unmute_failure_is_consumed_and_detaches_owner(
    load_overlay: Any,
    command: _FakeMediaPlayerCommand,
    initial_muted: bool,
    expected_request: int,
    expected_response: tuple[str, str, float, bool],
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    entity = instance.state.media_player_entity
    if initial_muted:
        entity.muted = True
        entity.volume = 0.0
        entity.previous_volume = 0.4
    session.volume_requests.clear()

    def fail_volume(percent: int) -> int:
        session.volume_requests.append(percent)
        raise RuntimeError("renderer unavailable")

    session.request_playback_volume = fail_volume

    responses = list(instance.handle_message(_player_command(command)))

    assert session.volume_requests == [expected_request]
    assert session.interrupted == 1
    assert session.interrupt_preserve_session == [False]
    assert session.state is _FakeSessionState.STOPPING
    assert instance._codex_realtime_owner is None
    assert entity.muted is initial_muted
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []
    assert entity.server.state.persisted_volumes == []
    assert responses == [expected_response]


def test_direct_volume_vendor_fallback_keeps_owner_decision_locked(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    original_vendor_handler = module._VENDOR_BASE_HANDLE_MESSAGE
    vendor_entered = threading.Event()
    release_vendor = threading.Event()
    contender_started = threading.Event()
    contender_acquired = threading.Event()

    def lose_owner(percent: int) -> int:
        session.terminal = True
        raise RuntimeError(f"owner lost before accepting {percent}")

    def blocked_vendor_handler(protocol_instance: Any, message: Any) -> Any:
        vendor_entered.set()
        if not release_vendor.wait(2):
            raise RuntimeError("test vendor fallback barrier timed out")
        yield from original_vendor_handler(protocol_instance, message)

    def contend_for_owner_lock() -> None:
        contender_started.set()
        with module._realtime_state_lock(instance):
            contender_acquired.set()

    session.request_playback_volume = lose_owner
    monkeypatch.setattr(module, "_VENDOR_BASE_HANDLE_MESSAGE", blocked_vendor_handler)
    responses: list[Any] = []
    handler = threading.Thread(
        target=lambda: responses.extend(instance.handle_message(_volume_command(0.8)))
    )
    contender = threading.Thread(target=contend_for_owner_lock)
    handler.start()
    assert vendor_entered.wait(1)
    contender.start()
    assert contender_started.wait(1)
    try:
        assert not contender_acquired.wait(0.05)
    finally:
        release_vendor.set()
        handler.join(1)
        contender.join(1)

    assert not handler.is_alive()
    assert not contender.is_alive()
    assert contender_acquired.is_set()
    assert responses == [("media-state", "idle", 0.8, False)]


def test_volume_without_live_owner_uses_unchanged_vendor_path(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()

    responses = list(instance.handle_message(_volume_command(0.8)))

    entity = instance.state.media_player_entity
    assert entity.music_player.volume_calls == [80]
    assert entity.announce_player.volume_calls == [80]
    assert entity.server.state.persisted_volumes == [0.8]
    assert responses == [("media-state", "idle", 0.8, False)]


def test_half_duplex_owner_uses_unchanged_vendor_volume_path(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(
        media_transport="bridge_pcm",
        full_duplex=False,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]

    list(instance.handle_message(_volume_command(0.8)))

    entity = instance.state.media_player_entity
    assert session.volume_requests == []
    assert entity.music_player.volume_calls == [80]
    assert entity.announce_player.volume_calls == [80]
    assert entity.server.state.persisted_volumes == [0.8]


def test_live_direct_mute_and_unmute_never_touch_physical_players(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(
        media_transport="device_webrtc",
        playback_volume_percent=50,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    entity = instance.state.media_player_entity
    entity.volume = 0.4
    entity.previous_volume = 0.4
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    session.volume_requests.clear()

    muted = list(instance.handle_message(_player_command(_FakeMediaPlayerCommand.MUTE)))
    unmuted = list(
        instance.handle_message(_player_command(_FakeMediaPlayerCommand.UNMUTE))
    )

    assert session.volume_requests == [0, 40]
    assert entity.volume == 0.4
    assert entity.previous_volume == 0.4
    assert entity.muted is False
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []
    assert entity.server.state.persisted_volumes == []
    assert muted == [("media-state", "idle", 0.0, True)]
    assert unmuted == [("media-state", "idle", 0.4, False)]


def test_live_direct_volume_while_muted_updates_saved_choice_but_stays_silent(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(
        media_transport="device_webrtc",
        playback_volume_percent=50,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    entity = instance.state.media_player_entity
    entity.volume = 0.4
    entity.previous_volume = 0.4
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    list(instance.handle_message(_player_command(_FakeMediaPlayerCommand.MUTE)))
    session.volume_requests.clear()

    responses = list(instance.handle_message(_volume_command(0.3)))

    assert session.volume_requests == [0]
    assert entity.volume == 0.0
    assert entity.previous_volume == 0.3
    assert entity.muted is True
    assert entity.server.state.persisted_volumes == [0.3]
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []
    assert responses == [("media-state", "idle", 0.0, True)]


@pytest.mark.parametrize(
    "message",
    [
        _FakeMediaPlayerCommandRequest(
            has_volume=True,
            volume=0.3,
            has_media_url=True,
        ),
        _FakeMediaPlayerCommandRequest(
            has_command=True,
            command=_FakeMediaPlayerCommand.PLAY,
            has_volume=True,
            volume=0.3,
        ),
        _FakeMediaPlayerCommandRequest(
            has_command=True,
            command=_FakeMediaPlayerCommand.MUTE,
            has_volume=True,
            volume=0.3,
        ),
    ],
)
def test_live_direct_ambiguous_volume_commands_fail_closed_before_vendor(
    load_overlay: Any,
    message: _FakeMediaPlayerCommandRequest,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    session.volume_requests.clear()

    responses = list(instance.handle_message(message))

    entity = instance.state.media_player_entity
    assert session.volume_requests == []
    assert entity.volume == 0.6
    assert entity.previous_volume == 0.6
    assert entity.muted is False
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []
    assert entity.server.state.persisted_volumes == []
    assert responses == [("media-state", "idle", 0.6, False)]


def test_realtime_init_prioritizes_and_tunes_only_configured_detector(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(wake_probability_cutoff=0.85)
    protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)

    instance = protocol()
    wake_words = instance.state.wake_words
    wake_words["okay_nabu"].probability_cutoff = 0.91

    assert isinstance(wake_words, module._RealtimeFirstWakeWords)
    assert list(wake_words) == ["okay_nabu", "okay_computer"]
    assert [wake_word.id for wake_word in wake_words.values()] == [
        "okay_computer",
        "okay_nabu",
    ]
    assert wake_words["okay_computer"].probability_cutoff == 0.85
    assert wake_words["okay_nabu"].probability_cutoff == 0.91

    wake_words["hey_jarvis"] = SimpleNamespace(
        id="hey_jarvis",
        wake_word="Hey Jarvis",
        probability_cutoff=0.90,
    )
    module._install_realtime_wake_order(instance.state)

    assert instance.state.wake_words is wake_words
    assert [wake_word.id for wake_word in wake_words.values()] == [
        "okay_computer",
        "okay_nabu",
        "hey_jarvis",
    ]


def test_personalized_wake_replaces_one_detector_without_running_both(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _fake_realtime_support(
        personalized_wake_config_path="/data/conf/codex-wakewords/custom.json"
    )
    _protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    closed: list[str] = []
    existing = SimpleNamespace(
        id="okay_computer",
        wake_word="Okay Computer",
        probability_cutoff=0.97,
        close=lambda: closed.append("vendor"),
    )
    other = SimpleNamespace(
        id="okay_nabu",
        wake_word="Okay Nabu",
        probability_cutoff=0.85,
    )
    personalized = SimpleNamespace(
        id="custom_model",
        wake_word="Okay Computer",
        probability_cutoff=0.81,
        close=lambda: closed.append("personalized"),
    )
    state = SimpleNamespace(wake_words={"okay_nabu": other, "okay_computer": existing})
    monkeypatch.setattr(
        module,
        "_load_personalized_wake",
        lambda path: personalized,
    )

    module._install_personalized_wake(state)
    module._install_realtime_wake_order(state)

    assert state.wake_words["okay_computer"] is personalized
    assert personalized.id == "okay_computer"
    assert len(state.wake_words) == 2
    assert closed == ["vendor"]
    assert [wake.id for wake in state.wake_words.values()] == [
        "okay_computer",
        "okay_nabu",
    ]


def test_personalized_wake_rejects_a_mismatched_phrase_and_closes_model(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _fake_realtime_support(
        personalized_wake_config_path="/data/conf/codex-wakewords/custom.json"
    )
    _protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    closed: list[str] = []
    personalized = SimpleNamespace(
        id="wrong",
        wake_word="Hey Jarvis",
        close=lambda: closed.append("personalized"),
    )
    state = SimpleNamespace(
        wake_words={
            "okay_computer": SimpleNamespace(
                id="okay_computer",
                wake_word="Okay Computer",
            )
        }
    )
    monkeypatch.setattr(
        module,
        "_load_personalized_wake",
        lambda path: personalized,
    )

    with pytest.raises(RuntimeError, match="phrase does not match"):
        module._install_personalized_wake(state)

    assert state.wake_words["okay_computer"] is not personalized
    assert closed == ["personalized"]


def test_realtime_init_installs_order_before_vendor_publication_and_late_add(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _fake_realtime_support(wake_probability_cutoff=None)
    _protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    nabu = SimpleNamespace(
        id="okay_nabu",
        wake_word="Okay Nabu",
        probability_cutoff=0.85,
    )
    computer = SimpleNamespace(
        id="okay_computer",
        wake_word="Okay Computer",
        probability_cutoff=0.97,
    )
    state = SimpleNamespace(wake_words={"okay_nabu": nabu})
    observed: list[bool] = []

    def vendor_init(_instance: Any, received_state: Any) -> None:
        observed.append(
            isinstance(received_state.wake_words, module._RealtimeFirstWakeWords)
        )

    monkeypatch.setattr(module, "_VENDOR_TR_INIT", vendor_init)
    module._fast_thirdreality_init(SimpleNamespace(), state)
    state.wake_words["okay_computer"] = computer

    assert observed == [True]
    assert [wake_word.id for wake_word in state.wake_words.values()] == [
        "okay_computer",
        "okay_nabu",
    ]
    assert computer.probability_cutoff == 0.97

    detector_without_cutoff = SimpleNamespace(
        id="okay_computer",
        wake_word="Okay Computer",
    )
    unsupported = module._RealtimeFirstWakeWords(
        {"okay_computer": detector_without_cutoff}
    )

    assert tuple(unsupported.values()) == (detector_without_cutoff,)
    assert not hasattr(detector_without_cutoff, "probability_cutoff")


def test_realtime_order_only_prioritizes_same_block_collision(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support()
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    ordered = tuple(protocol().state.wake_words.values())

    def first_activation(blocks: list[set[str]]) -> str | None:
        for ready_ids in blocks:
            for wake_word in ordered:
                if wake_word.id in ready_ids:
                    return wake_word.id
        return None

    assert first_activation([{"okay_nabu", "okay_computer"}]) == "okay_computer"
    assert first_activation([{"okay_nabu"}, {"okay_computer"}]) == "okay_nabu"


def test_wake_detector_syslog_uses_only_fixed_vocabulary(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _fake_realtime_support()
    protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    messages: list[tuple[int, str]] = []
    monkeypatch.setattr(
        module.syslog,
        "syslog",
        lambda priority, message: messages.append((priority, message)),
    )

    direct = protocol()
    normal = protocol()
    _wake(direct, "Okay Computer")
    _wake(normal, "Okay Nabu")

    assert messages == [
        (
            module.syslog.LOG_INFO,
            "codex-voice wake_detector=realtime selection=configured_phrase",
        ),
        (
            module.syslog.LOG_INFO,
            "codex-voice wake_detector=assist selection=normal_phrase",
        ),
    ]


def test_wake_detector_syslog_failure_and_invalid_values_are_non_disruptive(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _fake_realtime_support()
    protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    messages: list[tuple[int, str]] = []
    monkeypatch.setattr(
        module.syslog,
        "syslog",
        lambda priority, message: messages.append((priority, message)),
    )

    module._log_wake_selection("unknown", "configured_phrase")
    module._log_wake_selection("realtime", "unknown")

    assert messages == []

    def fail_syslog(_priority: int, _message: str) -> None:
        raise OSError("syslog unavailable")

    monkeypatch.setattr(module.syslog, "syslog", fail_syslog)
    instance = protocol()
    _wake(instance, "Okay Computer")

    assert len(support.sessions) == 1  # type: ignore[attr-defined]
    assert instance._codex_realtime_owner is not None


def test_realtime_only_routes_nabu_directly_and_blocks_assist_wakes(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _fake_realtime_support(
        wake_phrase="okay nabu",
        realtime_only=True,
    )
    protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    messages: list[tuple[int, str]] = []
    monkeypatch.setattr(
        module.syslog,
        "syslog",
        lambda priority, message: messages.append((priority, message)),
    )

    direct = protocol()
    blocked = protocol()
    _wake(direct, "Okay Nabu")
    _wake(blocked, "Okay Computer")

    assert len(support.sessions) == 1  # type: ignore[attr-defined]
    assert direct._codex_realtime_owner is not None
    assert direct.requests == []
    assert blocked.requests == []
    assert not blocked._pipeline_active
    assert not blocked._is_streaming_audio
    assert messages == [
        (
            module.syslog.LOG_INFO,
            "codex-voice wake_detector=realtime selection=configured_phrase",
        ),
        (
            module.syslog.LOG_INFO,
            "codex-voice wake_detector=disabled selection=realtime_only",
        ),
    ]


def test_realtime_only_bridge_failure_does_not_fall_back_to_assist(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(
        start_error=OSError("bridge unavailable"),
        wake_phrase="okay nabu",
        realtime_only=True,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()

    _wake(instance, "okay nabu")

    session = support.sessions[0]  # type: ignore[attr-defined]
    assert session.started == 1
    assert session.stopped == 1
    assert instance.requests == []
    assert instance.audio == []
    assert instance._codex_realtime_owner is None
    assert not instance._pipeline_active
    assert not instance._is_streaming_audio


def test_full_duplex_bridge_uses_deterministic_ready_boundary(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads_before = frozenset(threading.enumerate())
    aec3_capture = ModuleType("aec3_capture")
    aec3_capture.install_from_environment = (  # type: ignore[attr-defined]
        lambda *, environ: object()
    )
    monkeypatch.setitem(sys.modules, "aec3_capture", aec3_capture)
    support = _fake_realtime_support(
        media_transport="bridge_pcm",
        capture_backend=NATIVE_AEC3_CAPTURE,
        full_duplex=True,
        wake_phrase="okay nabu",
        realtime_only=True,
    )
    protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    led_states: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        module,
        "_nonblocking_led_fire",
        lambda state, to_idle=False: led_states.append((state, to_idle)),
    )
    instance = protocol()
    instance.state.active_wake_words.add("stop")
    instance.handle_audio(_pcm_frame(1))

    _wake(instance, "okay nabu")

    session = support.sessions[0]  # type: ignore[attr-defined]
    owner = instance._codex_realtime_owner
    assert owner is not None
    assert owner.startup_deadline is not None
    assert session.audio == []
    assert session.volume_requests == [60]
    assert support.prewarm_calls == []  # type: ignore[attr-defined]
    assert "stop" not in instance.state.active_wake_words
    assert led_states == [("thinking", False)]

    instance.handle_audio(_pcm_frame(2))
    assert session.audio == []

    _mark_direct_ready(instance, session)
    instance.handle_audio(_pcm_frame(3))
    assert session.audio == []
    assert instance.events == ["duck", "cue"]

    callback = instance.state.tts_player.callbacks.pop()
    callback()
    instance.handle_audio(_pcm_frame(4))

    assert owner.capture_open
    assert session.live_capture_opened == 1
    assert session.audio == [_pcm_frame(4)]
    assert led_states == [("thinking", False), ("listening", False)]

    list(instance.handle_message(_volume_command(0.4)))
    entity = instance.state.media_player_entity
    assert session.volume_requests == [60, 40]
    assert entity.music_player.volume_calls == []
    assert entity.announce_player.volume_calls == []

    session.terminal = True
    instance.handle_audio(_pcm_frame(5))
    assert instance._codex_realtime_owner is None
    assert "stop" in instance.state.active_wake_words
    for _ in range(100):
        if not any(
            thread not in threads_before
            and thread.name == "thirdreality-realtime-startup"
            for thread in threading.enumerate()
        ):
            break
        time.sleep(0.002)
    assert not any(
        thread not in threads_before and thread.name == "thirdreality-realtime-startup"
        for thread in threading.enumerate()
    )


def test_realtime_only_guard_mismatch_fails_closed_instead_of_using_assist(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(
        wake_phrase="okay nabu",
        realtime_only=True,
    )
    hashes = list(_REALTIME_HASHES)
    hashes[4] = "unknown"
    protocol, module, _tr_satellite = load_overlay(tuple(hashes), support)
    instance = protocol()

    _wake(instance, "okay nabu")

    assert module._REALTIME_PATCH_ACTIVE is False
    assert support.sessions == []  # type: ignore[attr-defined]
    assert instance.requests == []
    assert not instance._pipeline_active
    assert not instance._is_streaming_audio


def test_direct_webrtc_constructor_failure_does_not_start_ha_fallback(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _fake_realtime_support(
        media_transport="device_webrtc",
        constructor_error=RuntimeError("unavailable"),
    )
    protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    led_states: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        module,
        "_nonblocking_led_fire",
        lambda state, to_idle=False: led_states.append((state, to_idle)),
    )
    instance = protocol()

    _wake(instance, "okay computer")

    assert not instance.requests
    assert not instance.audio
    assert not instance._pipeline_active
    assert not instance._is_streaming_audio
    assert led_states == [("thinking", False), ("idle", True)]


@pytest.mark.parametrize("media_transport", ["device_webrtc", "bridge_pcm"])
def test_deterministic_transport_retries_pre_ready_failures_before_releasing_mic(
    load_overlay: Any,
    media_transport: str,
) -> None:
    support = _fake_realtime_support(
        media_transport=media_transport,
        full_duplex=True,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    preroll = _pcm_frame(7)
    instance.handle_audio(preroll)
    _wake(instance, "okay computer")
    first = support.sessions[0]  # type: ignore[attr-defined]
    first.failed_before_ready = True

    instance.handle_audio(_pcm_frame(8))
    for _ in range(100):
        if len(support.sessions) == 2:  # type: ignore[attr-defined]
            break
        time.sleep(0.002)

    second = support.sessions[1]  # type: ignore[attr-defined]
    assert first.stopped == 1
    assert instance._codex_realtime_owner.session is second
    assert instance._pipeline_active
    assert instance._is_streaming_audio
    assert second.audio == []
    assert instance.events == ["duck"]

    second.failed_before_ready = True
    instance.handle_audio(_pcm_frame(9))
    for _ in range(100):
        if len(support.sessions) == 3:  # type: ignore[attr-defined]
            break
        time.sleep(0.002)
    third = support.sessions[2]  # type: ignore[attr-defined]
    assert second.stopped == 1
    assert instance._codex_realtime_owner.session is third

    third.failed_before_ready = True
    instance.handle_audio(_pcm_frame(10))
    for _ in range(100):
        if instance._codex_realtime_owner is None:
            break
        time.sleep(0.002)

    assert third.stopped == 1
    assert not instance.requests
    assert not instance.audio
    assert not instance._pipeline_active
    assert not instance._is_streaming_audio


@pytest.mark.parametrize("media_transport", ["device_webrtc", "bridge_pcm"])
def test_deterministic_startup_deadline_is_shared_by_all_attempts(
    load_overlay: Any,
    media_transport: str,
) -> None:
    support = _fake_realtime_support(
        media_transport=media_transport,
        full_duplex=True,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    owner = instance._codex_realtime_owner
    owner.startup_deadline = 0.0

    instance.handle_audio(_pcm_frame(8))
    for _ in range(100):
        if instance._codex_realtime_owner is None:
            break
        time.sleep(0.002)

    assert session.stopped == 1
    assert len(support.sessions) == 1  # type: ignore[attr-defined]
    assert instance._codex_realtime_owner is None
    assert not instance._pipeline_active
    assert not instance._is_streaming_audio


def test_direct_lifecycle_watcher_starts_ready_cue_without_mic_callback(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    owner = instance._codex_realtime_owner

    session.ready_at = owner.startup_deadline - 1.0
    session.ready = True
    session.state = _FakeSessionState.READY

    for _ in range(100):
        if instance.state.tts_player.callbacks:
            break
        time.sleep(0.002)

    assert owner.ready_seen
    assert owner.ready_confirmation_pending
    assert instance.events == ["duck", "cue"]
    assert session.audio == []


def test_direct_lifecycle_watcher_retries_without_mic_callback(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    first = support.sessions[0]  # type: ignore[attr-defined]

    first.failed_before_ready = True
    first.terminal = True

    for _ in range(100):
        if len(support.sessions) == 2:  # type: ignore[attr-defined]
            break
        time.sleep(0.002)

    assert first.stopped == 1
    assert instance._codex_realtime_owner.session is support.sessions[1]  # type: ignore[attr-defined]
    assert instance._pipeline_active
    assert instance._is_streaming_audio


def test_direct_lifecycle_watcher_rejects_ready_after_deadline(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    owner = instance._codex_realtime_owner

    session.ready_at = owner.startup_deadline
    session.ready = True
    session.state = _FakeSessionState.READY

    for _ in range(100):
        if instance._codex_realtime_owner is None:
            break
        time.sleep(0.002)

    assert session.stopped == 1
    assert instance._codex_realtime_owner is None
    assert instance.state.tts_player.callbacks == []
    assert not instance._pipeline_active


def test_direct_webrtc_opens_capture_only_after_ready_cue_finishes(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(
        media_transport="device_webrtc",
        fallback_buffer_bytes=4,
        input_queue_bytes=64 * 1024,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    preroll = [_pcm_frame(1, samples=1_024), _pcm_frame(2, samples=1_024)]
    for frame in preroll:
        instance.handle_audio(frame)

    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    first_live = _pcm_frame(3)
    second_live = _pcm_frame(4)
    instance.handle_audio(first_live)
    instance.handle_audio(second_live)

    assert session.audio == []
    owner = instance._codex_realtime_owner
    assert owner is not None
    assert not owner.ready_seen
    assert not owner.capture_open
    assert list(owner.fallback_audio) == []
    assert owner.fallback_bytes == 0
    assert "stop" not in instance.state.active_wake_words

    _mark_direct_ready(instance, session)
    instance.handle_audio(_pcm_frame(5))

    assert session.audio == []
    assert owner.ready_seen
    assert owner.ready_confirmation_pending
    assert not owner.capture_open
    assert session.live_capture_opened == 0
    assert instance.events == ["duck", "cue"]

    callback = instance.state.tts_player.callbacks.pop()
    callback()
    instance.handle_audio(_pcm_frame(6))

    assert session.audio == [_pcm_frame(6)]
    assert owner.capture_open
    assert not owner.ready_confirmation_pending
    assert session.live_capture_opened == 1
    assert "stop" not in instance.state.active_wake_words
    assert not instance.requests
    assert not instance.audio
    assert instance._pipeline_active
    assert instance._is_streaming_audio


def test_direct_ready_cue_duplicate_callback_is_a_noop(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    led_states: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        module,
        "_nonblocking_led_fire",
        lambda state, to_idle=False: led_states.append((state, to_idle)),
    )
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    _mark_direct_ready(instance, session)
    callback = instance.state.tts_player.callbacks.pop()

    callback()
    callback()

    assert instance._codex_realtime_owner.capture_open
    assert led_states == [("thinking", False), ("listening", False)]


def test_direct_ready_cue_late_callback_fails_closed(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    now = [10.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    _mark_direct_ready(instance, session)
    callback = instance.state.tts_player.callbacks.pop()
    now[0] = 13.0

    callback()

    assert session.interrupted == 1
    assert instance._codex_realtime_owner is None
    assert not instance._pipeline_active


def test_direct_ready_cue_timeout_closes_without_mic_callback(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    _mark_direct_ready(instance, session)
    owner = instance._codex_realtime_owner
    owner.ready_confirmation_deadline = 0.0

    for _ in range(100):
        if instance._codex_realtime_owner is None:
            break
        time.sleep(0.002)

    assert session.interrupted == 1
    assert instance._codex_realtime_owner is None
    assert instance.events.count("stop") == 1
    assert not instance._pipeline_active


def test_direct_terminal_during_ready_cue_never_retries(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    _mark_direct_ready(instance, session)
    session.terminal = True

    for _ in range(100):
        if instance._codex_realtime_owner is None:
            break
        time.sleep(0.002)

    assert len(support.sessions) == 1  # type: ignore[attr-defined]
    assert instance._codex_realtime_owner is None
    assert instance.events.count("stop") == 1
    assert not instance._pipeline_active


def test_direct_lifecycle_watcher_closes_live_owner_without_mic_callback(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    _mark_direct_ready(instance, session)
    callback = instance.state.tts_player.callbacks.pop()
    callback()

    assert instance._codex_realtime_owner.capture_open
    session.terminal = True

    for _ in range(100):
        if instance._codex_realtime_owner is None:
            break
        time.sleep(0.002)

    assert len(support.sessions) == 1  # type: ignore[attr-defined]
    assert instance._codex_realtime_owner is None
    assert instance.events.count("unduck") == 1
    assert not instance._pipeline_active
    assert not instance._is_streaming_audio


def test_failed_live_socket_reconnects_without_releasing_owner_or_replaying_cue(
    load_overlay: Any,
) -> None:
    """An unexpected live transport failure keeps LED/capture ownership alive."""
    support = _fake_realtime_support(
        media_transport="bridge_pcm",
        capture_backend="pulseaudio_aec",
        full_duplex=True,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    first = support.sessions[0]  # type: ignore[attr-defined]
    _mark_direct_ready(instance, first)
    instance.state.tts_player.callbacks.pop()()
    owner = instance._codex_realtime_owner

    first.state = _FakeSessionState.FAILED
    first.terminal_reason = "transport_closed"
    first.terminal = True
    for _ in range(100):
        if len(support.sessions) == 2:  # type: ignore[attr-defined]
            break
        time.sleep(0.002)

    replacement = support.sessions[1]  # type: ignore[attr-defined]
    assert instance._codex_realtime_owner is owner
    assert owner.session is replacement
    assert owner.capture_open
    assert instance.events == ["duck", "cue"]

    retained = _pcm_frame(12)
    instance.handle_audio(retained)
    assert replacement.audio == [retained]
    assert not owner.ready_seen

    replacement.ready_at = time.monotonic()
    replacement.ready = True
    replacement.state = _FakeSessionState.READY
    for _ in range(100):
        if owner.ready_seen:
            break
        time.sleep(0.002)
    assert owner.ready_seen
    assert owner.reconnect_attempt == 0
    assert instance.state.tts_player.callbacks == []

    instance.stop()


def test_live_policy_deadline_does_not_reconnect(
    load_overlay: Any,
) -> None:
    """An intentional idle limit releases ownership instead of reopening it."""
    support = _fake_realtime_support(
        media_transport="bridge_pcm",
        capture_backend="pulseaudio_aec",
        full_duplex=True,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    _mark_direct_ready(instance, session)
    instance.state.tts_player.callbacks.pop()()

    session.state = _FakeSessionState.FAILED
    session.terminal_reason = "idle_deadline"
    session.terminal = True
    for _ in range(100):
        if instance._codex_realtime_owner is None:
            break
        time.sleep(0.002)

    assert len(support.sessions) == 1  # type: ignore[attr-defined]
    assert instance._codex_realtime_owner is None
    assert instance.events.count("unduck") == 1


def test_direct_webrtc_real_input_queue_full_fails_closed(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    session.submit_result = support.SubmitResult.FULL  # type: ignore[attr-defined]
    _mark_direct_ready(instance, session)
    instance.handle_audio(_pcm_frame(1))
    callback = instance.state.tts_player.callbacks.pop()
    callback()

    instance.handle_audio(_pcm_frame(2))

    assert session.stopped == 0
    assert session.interrupted == 1
    assert instance._codex_realtime_owner is None
    assert not instance.requests
    assert not instance.audio
    assert not instance._pipeline_active
    assert not instance._is_streaming_audio


def test_realtime_wake_claims_mic_without_starting_home_assistant(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support()
    protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()

    _wake(instance, "  OKAY   Computer ")

    assert module._REALTIME_PATCH_ACTIVE
    assert len(support.sessions) == 1  # type: ignore[attr-defined]
    session = support.sessions[0]  # type: ignore[attr-defined]
    assert session.started == 1
    assert instance.events == ["duck"]
    assert not instance.requests
    assert instance._pipeline_active
    assert instance._is_streaming_audio
    assert "stop" in instance.state.active_wake_words

    instance.handle_audio(b"\x01\x00" * 8)
    assert session.audio == [b"\x01\x00" * 8]
    assert not instance.audio


def test_realtime_wake_prepends_only_bounded_idle_preroll(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support()
    protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    idle_frames = [_pcm_frame(index, samples=1_024) for index in range(1, 9)]
    post_wake = _pcm_frame(9)

    for frame in idle_frames:
        instance.handle_audio(frame)
    _wake(instance, "okay computer")
    instance.handle_audio(post_wake)

    session = support.sessions[0]  # type: ignore[attr-defined]
    assert module._REALTIME_PREROLL_MAX_BYTES == 12 * 1024
    assert session.audio == [*idle_frames[-6:], post_wake]
    assert list(instance._codex_realtime_owner.fallback_audio) == [
        *idle_frames[-6:],
        post_wake,
    ]
    assert not instance.audio


def test_normal_wake_discards_direct_preroll(load_overlay: Any) -> None:
    support = _fake_realtime_support()
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()

    instance.handle_audio(_pcm_frame(1))
    _wake(instance, "okay nabu")
    post_wake = _pcm_frame(2)
    instance.handle_audio(post_wake)

    assert not support.sessions  # type: ignore[attr-defined]
    assert instance.audio == [post_wake]
    assert instance._codex_realtime_preroll is None


def test_wake_detector_false_positive_cannot_preempt_realtime_session(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support()
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()

    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    owner = instance._codex_realtime_owner
    assert not session.terminal
    _wake(instance, "okay nabu")

    assert session.interrupted == 0
    assert session.interrupt_preserve_session == []
    assert instance._codex_realtime_owner is owner
    assert instance.requests == []
    assert instance.events == ["duck"]
    assert instance._pipeline_active
    assert instance._is_streaming_audio
    assert "stop" in instance.state.active_wake_words

    instance.handle_audio(b"\x01\x00" * 8)
    assert session.audio == [b"\x01\x00" * 8]


def test_terminal_owner_is_reaped_before_starting_next_realtime_wake(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _fake_realtime_support()
    protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    led_states: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        module,
        "_nonblocking_led_fire",
        lambda state, to_idle=False: led_states.append((state, to_idle)),
    )
    instance = protocol()
    _wake(instance, "okay computer")
    stale_owner = instance._codex_realtime_owner
    stale_session = support.sessions[0]  # type: ignore[attr-defined]
    instance.handle_audio(_pcm_frame(1))
    stale_session.terminal = True

    _wake(instance, "okay computer")

    assert len(support.sessions) == 2  # type: ignore[attr-defined]
    assert stale_owner.released
    assert list(stale_owner.fallback_audio) == []
    assert stale_owner.fallback_bytes == 0
    assert instance._codex_realtime_owner is not stale_owner
    assert instance._codex_realtime_owner.session is support.sessions[1]  # type: ignore[attr-defined]
    assert instance.events == ["duck", "unduck", "duck"]
    assert led_states == [
        ("listening", False),
        ("idle", True),
        ("listening", False),
    ]
    assert instance._pipeline_active
    assert instance._is_streaming_audio
    assert "stop" in instance.state.active_wake_words


def test_terminal_owner_cleanup_on_next_wake_is_idempotently_idle(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    support = _fake_realtime_support()
    protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    led_states: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        module,
        "_nonblocking_led_fire",
        lambda state, to_idle=False: led_states.append((state, to_idle)),
    )
    instance = protocol()
    _wake(instance, "okay computer")
    stale_owner = instance._codex_realtime_owner
    stale_owner.session.terminal = True
    instance.state.muted = True

    _wake(instance, "okay computer")
    module._detach_realtime_owner(instance, stale_owner, unduck=True)
    instance.stop()

    assert len(support.sessions) == 1  # type: ignore[attr-defined]
    assert stale_owner.released
    assert instance._codex_realtime_owner is None
    assert not instance._pipeline_active
    assert not instance._is_streaming_audio
    assert "stop" not in instance.state.active_wake_words
    assert instance.events.count("unduck") == 1
    assert led_states.count(("idle", True)) == 1


def test_realtime_start_exception_falls_back_on_same_wake_call(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(start_error=OSError("bridge unavailable"))
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()

    _wake(instance, "okay computer")

    session = support.sessions[0]  # type: ignore[attr-defined]
    assert session.started == 1
    assert session.stopped == 1
    assert instance.requests == ["okay computer"]
    assert instance.events == ["request", "duck"]
    assert instance._codex_realtime_owner is None
    assert instance._pipeline_active
    assert instance._is_streaming_audio
    assert "stop" not in instance.state.active_wake_words


def test_realtime_start_exception_replays_idle_preroll_once(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(start_error=OSError("bridge unavailable"))
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    preroll = [_pcm_frame(1), _pcm_frame(2)]

    for chunk in preroll:
        instance.handle_audio(chunk)
    _wake(instance, "okay computer")

    session = support.sessions[0]  # type: ignore[attr-defined]
    assert session.started == 1
    assert session.stopped == 1
    assert session.audio == []
    assert instance.audio == preroll
    assert instance.requests == ["okay computer"]
    assert instance._codex_realtime_owner is None


def test_realtime_constructor_exception_preserves_vendor_state_and_preroll(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(
        constructor_error=OSError("client construction failed")
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    preroll = [_pcm_frame(1), _pcm_frame(2)]

    for chunk in preroll:
        instance.handle_audio(chunk)
    _wake(instance, "okay computer")

    assert not support.sessions  # type: ignore[attr-defined]
    assert instance.requests == ["okay computer"]
    assert instance.audio == preroll
    assert "stop" not in instance.state.active_wake_words
    assert getattr(instance, "_codex_realtime_owner", None) is None
    assert instance._pipeline_active
    assert instance._is_streaming_audio


def test_partial_direct_duck_failure_is_undone_before_ha_fallback(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support()
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    preroll = _pcm_frame(1)
    instance.handle_audio(preroll)
    duck_calls = 0

    def duck_once_then_succeed() -> None:
        nonlocal duck_calls
        duck_calls += 1
        instance.events.append("duck")
        if duck_calls == 1:
            raise RuntimeError("partial duck failure")

    instance.duck = duck_once_then_succeed
    _wake(instance, "okay computer")

    session = support.sessions[0]  # type: ignore[attr-defined]
    assert session.stopped == 1
    assert instance.events == ["duck", "unduck", "request", "duck", "audio"]
    assert instance.audio == [preroll]
    assert instance._codex_realtime_owner is None
    assert instance._pipeline_active
    assert instance._is_streaming_audio


def test_small_legal_queues_reserve_live_headroom_instead_of_seeding_preroll(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(
        fallback_buffer_bytes=16 * 1024,
        input_queue_bytes=32 * 1024,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    idle_frames = [_pcm_frame(index, samples=1_024) for index in range(1, 7)]

    for frame in idle_frames:
        instance.handle_audio(frame)
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]

    assert session.audio == []
    assert instance._codex_realtime_owner.fallback_bytes == 0
    assert instance._codex_realtime_owner is not None


def test_pre_ready_failure_replays_bounded_pcm_to_home_assistant_in_order(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(fallback_buffer_bytes=4)
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]

    instance.handle_audio(b"\x01\x00\x02\x00")
    # Overflow is detected on the vendor mic thread. The current block is not
    # submitted direct and is replayed after the buffered block into HA.
    instance.handle_audio(b"\x03\x00")

    assert session.audio == [b"\x01\x00\x02\x00"]
    assert session.stopped == 1
    assert instance.requests == ["okay computer"]
    assert instance.audio == [b"\x01\x00\x02\x00", b"\x03\x00"]
    assert instance.events == [
        "duck",
        "unduck",
        "request",
        "duck",
        "audio",
        "audio",
    ]
    assert instance._codex_realtime_owner is None


def test_async_start_failure_replays_current_frame_and_marks_normal_owner(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support()
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    instance.handle_audio(b"\x01\x00")
    session.failed_before_ready = True
    session.terminal = True

    instance.handle_audio(b"\x02\x00")

    assert session.stopped == 1
    assert instance.audio == [b"\x01\x00", b"\x02\x00"]
    assert instance.requests == ["okay computer"]
    assert instance._codex_realtime_owner is None


def test_async_start_failure_replays_preroll_and_live_audio_once(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support()
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    preroll = [_pcm_frame(1), _pcm_frame(2)]

    for chunk in preroll:
        instance.handle_audio(chunk)
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    live = _pcm_frame(3)
    trailing = _pcm_frame(4)
    instance.handle_audio(live)
    session.failed_before_ready = True
    session.terminal = True
    instance.handle_audio(trailing)

    assert session.audio == [*preroll, live]
    assert session.stopped == 1
    assert instance.audio == [*preroll, live, trailing]
    assert instance.requests == ["okay computer"]
    assert instance._codex_realtime_owner is None


@pytest.mark.parametrize("invalid_state", ["muted", "disconnected"])
def test_invalid_idle_state_clears_realtime_preroll(
    load_overlay: Any,
    invalid_state: str,
) -> None:
    support = _fake_realtime_support()
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    instance.handle_audio(_pcm_frame(1))
    if invalid_state == "muted":
        instance.state.muted = True
    else:
        instance.state.connected = False

    instance.handle_audio(_pcm_frame(2))
    instance.state.muted = False
    instance.state.connected = True
    _wake(instance, "okay computer")

    session = support.sessions[0]  # type: ignore[attr-defined]
    assert session.audio == []


def test_ownerless_stop_clears_idle_preroll(load_overlay: Any) -> None:
    support = _fake_realtime_support()
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    instance.handle_audio(_pcm_frame(1))

    instance.stop()
    _wake(instance, "okay computer")

    session = support.sessions[0]  # type: ignore[attr-defined]
    assert session.audio == []
    assert instance._codex_realtime_preroll is None


@pytest.mark.parametrize("stop_word_preexisting", [False, True])
def test_stop_requested_during_start_cannot_leave_an_orphan_session(
    load_overlay: Any,
    stop_word_preexisting: bool,
) -> None:
    start_entered = threading.Event()
    start_release = threading.Event()
    stop_completed = threading.Event()
    support = _fake_realtime_support(
        start_entered=start_entered,
        start_release=start_release,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    if stop_word_preexisting:
        instance.state.active_wake_words.add("stop")

    wake_thread = threading.Thread(
        target=_wake,
        args=(instance, "okay computer"),
    )
    wake_thread.start()
    assert start_entered.wait(1)
    stop_thread = threading.Thread(
        target=lambda: (instance.stop(), stop_completed.set()),
    )
    stop_thread.start()
    assert not stop_completed.wait(0.05)
    start_release.set()
    wake_thread.join(1)
    stop_thread.join(1)

    session = support.sessions[0]  # type: ignore[attr-defined]
    assert not wake_thread.is_alive()
    assert not stop_thread.is_alive()
    assert stop_completed.is_set()
    assert session.started == 1
    assert session.interrupted == 1
    assert instance._codex_realtime_owner is None
    assert not instance._pipeline_active
    assert not instance._is_streaming_audio
    assert not instance.requests
    assert ("stop" in instance.state.active_wake_words) is stop_word_preexisting


def test_stop_requested_during_submit_cannot_resurrect_ha_fallback(
    load_overlay: Any,
) -> None:
    submit_entered = threading.Event()
    submit_release = threading.Event()
    stop_completed = threading.Event()
    support = _fake_realtime_support(
        submit_entered=submit_entered,
        submit_release=submit_release,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    session.submit_result = support.SubmitResult.FULL  # type: ignore[attr-defined]

    audio_thread = threading.Thread(
        target=instance.handle_audio,
        args=(_pcm_frame(1),),
    )
    audio_thread.start()
    assert submit_entered.wait(1)
    stop_thread = threading.Thread(
        target=lambda: (instance.stop(), stop_completed.set()),
    )
    stop_thread.start()
    assert not stop_completed.wait(0.05)
    submit_release.set()
    audio_thread.join(1)
    stop_thread.join(1)

    assert not audio_thread.is_alive()
    assert not stop_thread.is_alive()
    assert stop_completed.is_set()
    assert session.interrupted == 1
    assert session.stopped == 0
    assert instance._codex_realtime_owner is None
    assert not instance._pipeline_active
    assert not instance._is_streaming_audio
    assert not instance.requests


def test_stop_during_fallback_handoff_explicitly_cancels_ha_start(
    load_overlay: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff_entered = threading.Event()
    handoff_release = threading.Event()
    stop_completed = threading.Event()
    support = _fake_realtime_support()
    protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]
    session.failed_before_ready = True
    original_fast_wakeup = module._fast_wakeup

    def blocked_fast_wakeup(protocol_instance: Any, wake_word: Any) -> None:
        handoff_entered.set()
        assert handoff_release.wait(2)
        original_fast_wakeup(protocol_instance, wake_word)

    monkeypatch.setattr(module, "_fast_wakeup", blocked_fast_wakeup)
    audio_thread = threading.Thread(
        target=instance.handle_audio,
        args=(_pcm_frame(1),),
    )
    audio_thread.start()
    assert handoff_entered.wait(1)
    stop_thread = threading.Thread(
        target=lambda: (instance.stop(), stop_completed.set()),
    )
    stop_thread.start()
    assert not stop_completed.wait(0.05)
    handoff_release.set()
    audio_thread.join(1)
    stop_thread.join(1)

    assert not audio_thread.is_alive()
    assert not stop_thread.is_alive()
    assert stop_completed.is_set()
    assert session.stopped == 1
    assert instance.requests == ["okay computer"]
    assert instance.events.count("cancel") == 1
    assert instance._codex_realtime_owner is None
    assert not instance._pipeline_active
    assert not instance._is_streaming_audio


@pytest.mark.parametrize("stop_word_preexisting", [False, True])
def test_direct_stop_is_idempotently_idle_and_restores_stop_membership(
    load_overlay: Any,
    stop_word_preexisting: bool,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    if stop_word_preexisting:
        instance.state.active_wake_words.add("stop")
    _wake(instance, "okay computer")
    owner = instance._codex_realtime_owner
    session = support.sessions[0]  # type: ignore[attr-defined]
    assert "stop" not in instance.state.active_wake_words

    instance.stop()
    module._detach_realtime_owner(instance, owner, unduck=True)

    assert session.interrupted == 1
    assert instance._codex_realtime_owner is None
    assert not instance._pipeline_active
    assert not instance._is_streaming_audio
    assert instance.events.count("unduck") == 1
    assert ("stop" in instance.state.active_wake_words) is stop_word_preexisting


def test_direct_owner_suspends_legacy_stop_detector_for_entire_session(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()
    instance.state.active_wake_words.add("stop")

    _wake(instance, "okay computer")
    owner = instance._codex_realtime_owner
    session = support.sessions[0]  # type: ignore[attr-defined]

    # The wake tail must not be eligible for classification as a local stop
    # while the sidecar offer and bridge handshake are still starting.
    assert "stop" not in instance.state.active_wake_words
    assert session.interrupted == 0
    assert instance._codex_realtime_owner is owner
    assert instance._pipeline_active
    assert instance._is_streaming_audio

    _mark_direct_ready(instance, session)
    callback = instance.state.tts_player.callbacks.pop()
    callback()

    # Provider playback echo must remain unable to turn a LIVE detection into
    # a terminal local stop after capture opens.
    assert "stop" not in instance.state.active_wake_words

    session.terminal = True
    instance.handle_audio(_pcm_frame(2))

    assert instance._codex_realtime_owner is None
    assert "stop" in instance.state.active_wake_words


def test_half_duplex_owner_retains_terminal_stop_detector(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(
        media_transport="bridge_pcm",
        full_duplex=False,
    )
    protocol, _module, _tr_satellite = load_overlay(_REALTIME_HASHES, support)
    instance = protocol()

    _wake(instance, "okay computer")
    session = support.sessions[0]  # type: ignore[attr-defined]

    assert "stop" in instance.state.active_wake_words

    session.terminal = True
    instance.handle_audio(_pcm_frame(1))

    assert instance._codex_realtime_owner is None
    assert "stop" not in instance.state.active_wake_words


def test_realtime_opcode_mismatch_keeps_latency_patch_and_normal_audio_path(
    load_overlay: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    support = _fake_realtime_support()
    hashes = list(_REALTIME_HASHES)
    hashes[4] = "unknown"

    with caplog.at_level(logging.WARNING):
        protocol, module, tr_satellite = load_overlay(tuple(hashes), support)

    assert module._REALTIME_PATCH_ACTIVE is False
    assert _FakeProtocol.handle_audio is _VENDOR_BASE_HANDLE_AUDIO
    assert _FakeProtocol.stop is _VENDOR_BASE_STOP
    assert _FakeProtocol.handle_message is _VENDOR_BASE_HANDLE_MESSAGE
    assert _FakeTRProtocol.__init__ is _VENDOR_TR_INIT
    assert _FakeTRProtocol.wakeup is module._fast_thirdreality_wakeup
    assert tr_satellite._led_fire is module._nonblocking_led_fire  # type: ignore[attr-defined]
    assert (
        "Skipping ThirdReality realtime client: unrecognized vendor bytecode"
        in caplog.messages
    )

    instance = protocol()
    _wake(instance, "okay computer")
    instance.handle_audio(b"normal")
    assert instance.requests == ["okay computer"]
    assert instance.audio == [b"normal"]
    assert not support.sessions  # type: ignore[attr-defined]


@pytest.mark.parametrize("guard_index", range(6, len(_REALTIME_HASHES)))
def test_wake_arbitration_guard_mismatch_skips_constructor_patch(
    load_overlay: Any,
    guard_index: int,
) -> None:
    support = _fake_realtime_support()
    hashes = list(_REALTIME_HASHES)
    hashes[guard_index] = "unknown"

    _protocol, module, tr_satellite = load_overlay(tuple(hashes), support)

    assert module._REALTIME_PATCH_ACTIVE is False
    assert _FakeTRProtocol.__init__ is _VENDOR_TR_INIT
    assert _FakeTRProtocol.handle_message is _VENDOR_TR_HANDLE_MESSAGE
    assert _FakeTRProtocol._sync_volume_from_system is (
        _VENDOR_TR_SYNC_VOLUME_FROM_SYSTEM
    )
    assert _FakeTRProtocol._sync_state_from_system is _VENDOR_TR_SYNC_STATE_FROM_SYSTEM
    assert _FakeTRProtocol._system_sync_loop is _VENDOR_TR_SYSTEM_SYNC_LOOP
    assert _FakeMediaPlayerEntity.apply_volume_from_state is (
        _VENDOR_MEDIA_PLAYER_APPLY_VOLUME_FROM_STATE
    )
    assert tr_satellite._VOLUME_POLL_INTERVAL == 0.5  # type: ignore[attr-defined]


@pytest.mark.parametrize("guard_index", range(9, len(_REALTIME_HASHES)))
def test_media_volume_semantics_hash_mismatch_skips_message_patch(
    load_overlay: Any,
    guard_index: int,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    hashes = list(_REALTIME_HASHES)
    hashes[guard_index] = "unknown"

    protocol, module, tr_satellite = load_overlay(tuple(hashes), support)
    instance = protocol()
    list(instance.handle_message(_volume_command(0.8)))

    entity = instance.state.media_player_entity
    assert module._REALTIME_PATCH_ACTIVE is False
    assert _FakeProtocol.handle_message is _VENDOR_BASE_HANDLE_MESSAGE
    assert _FakeTRProtocol._sync_volume_from_system is (
        _VENDOR_TR_SYNC_VOLUME_FROM_SYSTEM
    )
    assert tr_satellite._VOLUME_POLL_INTERVAL == 0.5  # type: ignore[attr-defined]
    assert entity.music_player.volume_calls == [80]
    assert entity.announce_player.volume_calls == [80]
    assert entity.server.state.persisted_volumes == [0.8]


def test_volume_callback_hash_mismatch_fails_closed_before_message_patch(
    load_overlay: Any,
) -> None:
    support = _fake_realtime_support(media_transport="device_webrtc")
    hashes = list(_REALTIME_HASHES)
    hashes[_REALTIME_HASHES.index(_MEDIA_PLAYER_SET_VOLUME_CALLBACK_HASH)] = "unknown"

    protocol, module, _tr_satellite = load_overlay(tuple(hashes), support)
    instance = protocol()
    list(instance.handle_message(_volume_command(0.8)))

    entity = instance.state.media_player_entity
    assert module._REALTIME_PATCH_ACTIVE is False
    assert _FakeProtocol.handle_message is _VENDOR_BASE_HANDLE_MESSAGE
    assert _FakeTRProtocol._sync_volume_to_system is _VENDOR_TR_SYNC_VOLUME
    assert entity.music_player.volume_calls == [80]
    assert entity.announce_player.volume_calls == [80]


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
    assert _FakeTRProtocol.wakeup is _VENDOR_TR_WAKEUP
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
