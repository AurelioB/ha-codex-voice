# ruff: noqa: INP001
"""Apply process-local latency tuning for ThirdReality firmware v1.1.7."""

from __future__ import annotations

import atexit
import fcntl
import hashlib
import importlib
import json
import logging
import marshal
import math
import os
import stat
import subprocess
import syslog
import tempfile
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import replace
from enum import Enum, auto
from pathlib import Path
from typing import Any, NoReturn

_AEC3_TRUE_FLAGS = frozenset({"1", "true", "yes", "on"})
_AEC3_FALSE_FLAGS = frozenset({"", "0", "false", "no", "off"})


def _aec3_environment_override() -> bool:
    """Parse the explicit native-capture override without accepting typos."""
    setting = os.environ.get("CODEX_AEC3_CAPTURE", "").strip().lower()
    if setting in _AEC3_TRUE_FLAGS:
        return True
    if setting in _AEC3_FALSE_FLAGS:
        return False
    raise ValueError(
        "CODEX_AEC3_CAPTURE must be one of 0/1, false/true, no/yes, off/on"
    )


def _fatal_aec3_startup(message: str, cause: Exception | None = None) -> NoReturn:
    """Terminate outside ``site``'s ordinary-Exception recovery boundary."""
    os.environ.pop("CODEX_AEC3_ACTIVE", None)
    if cause is not None:
        raise SystemExit(message) from cause
    raise SystemExit(message)


# Native AEC3 must replace SoundCard's default microphone before the vendor
# entrypoint imports ``soundcard`` and resolves that callable. An explicitly
# selected but invalid native runtime aborts startup instead of exposing raw
# microphone audio.
_AEC3_CAPTURE_PATCH: Any = None
# This variable is proof produced by this process, not a supported service
# input. Discard an inherited or operator-supplied value before selection so a
# skipped or failed install cannot satisfy the later realtime preflight.
os.environ.pop("CODEX_AEC3_ACTIVE", None)
try:
    _AEC3_OVERRIDE = _aec3_environment_override()
except Exception as exc:  # noqa: BLE001 - parse failure must escape site recovery
    _fatal_aec3_startup("ThirdReality realtime configuration is invalid", exc)
try:
    _EARLY_REALTIME_SUPPORT: Any = importlib.import_module("realtime_client")
    _EARLY_REALTIME_CONFIG: Any = _EARLY_REALTIME_SUPPORT.load_config()
except FileNotFoundError:
    _EARLY_REALTIME_SUPPORT = None
    _EARLY_REALTIME_CONFIG = None
except Exception as exc:  # noqa: BLE001 - optional support is an untrusted boundary
    if _AEC3_OVERRIDE:
        _fatal_aec3_startup("ThirdReality realtime configuration is invalid", exc)
    logging.getLogger("linux_voice_assistant.realtime").warning(
        "ThirdReality realtime configuration is invalid"
    )
    _EARLY_REALTIME_SUPPORT = None
    _EARLY_REALTIME_CONFIG = None

_VALID_REALTIME_CONFIG = _EARLY_REALTIME_CONFIG is not None
if _AEC3_OVERRIDE and not _VALID_REALTIME_CONFIG:
    _fatal_aec3_startup(
        "CODEX_AEC3_CAPTURE requires a valid enabled realtime configuration"
    )
if (
    _AEC3_OVERRIDE
    and _EARLY_REALTIME_CONFIG.capture_backend
    != _EARLY_REALTIME_SUPPORT.NATIVE_AEC3_CAPTURE
):
    try:
        _EARLY_REALTIME_CONFIG = replace(
            _EARLY_REALTIME_CONFIG,
            capture_backend=_EARLY_REALTIME_SUPPORT.NATIVE_AEC3_CAPTURE,
        )
    except Exception as exc:  # noqa: BLE001 - native selection must fail closed
        _fatal_aec3_startup("ThirdReality realtime configuration is invalid", exc)
_NATIVE_AEC3_SELECTED = bool(
    _VALID_REALTIME_CONFIG
    and _EARLY_REALTIME_CONFIG.capture_backend
    == _EARLY_REALTIME_SUPPORT.NATIVE_AEC3_CAPTURE
)
if _NATIVE_AEC3_SELECTED:
    try:
        from aec3_capture import install_from_environment

        aec3_environment = dict(os.environ)
        aec3_environment["CODEX_AEC3_CAPTURE"] = "1"
        _AEC3_CAPTURE_PATCH = install_from_environment(environ=aec3_environment)
    except Exception as exc:  # enabled native capture must fail closed
        logging.getLogger("linux_voice_assistant.aec3").exception(
            "ThirdReality native AEC3 capture could not be installed"
        )
        _fatal_aec3_startup(
            "ThirdReality native AEC3 capture could not be installed",
            exc,
        )
    if _AEC3_CAPTURE_PATCH is None:
        _fatal_aec3_startup("ThirdReality native AEC3 capture was not enabled")
    os.environ["CODEX_AEC3_ACTIVE"] = "1"

try:
    from aioesphomeapi.api_pb2 import MediaPlayerCommandRequest, VoiceAssistantRequest
    from aioesphomeapi.model import MediaPlayerCommand
    from linux_voice_assistant.entity import MediaPlayerEntity
    from linux_voice_assistant.models import ServerState
    from linux_voice_assistant.satellite import VoiceSatelliteProtocol
    from thirdreality import satellite as thirdreality_satellite
except Exception as exc:
    if _NATIVE_AEC3_SELECTED:
        _fatal_aec3_startup(
            "ThirdReality native AEC3 could not load the guarded vendor runtime",
            exc,
        )
    raise

_LOGGER = logging.getLogger("linux_voice_assistant.satellite")
_LED_LOGGER = thirdreality_satellite._LOGGER  # noqa: SLF001

# Guard all monkeypatches with the exact installed v1.1.7 bytecode. The
# package metadata on the measured image reports an unrelated version, so it
# is not authoritative. Keeping this guard atomic prevents a mixed wake/LED
# implementation if either vendor module changes.
_EXPECTED_BASE_WAKEUP = (
    "9fc5d4920ced216444adf048f0733929a3261ae47a76ed5fa2bed8061cc46697"
)
_EXPECTED_BASE_FINISH = (
    "a1544719b6fac5cff4388a5c10f0674cd295fb98c3c86e799993db1cbee2080d"
)
_EXPECTED_TR_WAKEUP = "4aff556b90696a3b425978641a48022021b9ffd13f4176c6bed93963577df424"
_EXPECTED_TR_LED_FIRE = (
    "bd6ddee49d623fff2224b5ec0dfb302075d0be9ce3c245f6cf1cf993478f9efc"
)
_EXPECTED_BASE_HANDLE_AUDIO = (
    "f24c0428291b4155a3d1c8f62563f7480503d25a39b0bd7643c04546938e5b83"
)
_EXPECTED_BASE_STOP = "46827b29f17d65de0561b8d89f36fed99c61fa5e75c6359af6545db389972f8f"
_EXPECTED_BASE_HANDLE_MESSAGE = (
    "d930b8d7852ac6567b219119b3ac29599df0f87f39f6ad92beb9cd27cb678724"
)
_EXPECTED_TR_INIT = "9120bc4f5b727f360bdd632bd0fef25747a299ad64aabc5ec0bd57ac299eb24b"
_EXPECTED_TR_HANDLE_MESSAGE = (
    "8795319058e8b5e353b0ea7e056e8afeceead2587d4db2e16822880e065ddb8e"
)
_EXPECTED_TR_SYNC_VOLUME = (
    "41630a35da4a1f6dadccff70bb8bdc38acbb547c16966ba754ed33b461e73686"
)
_EXPECTED_TR_SYNC_VOLUME_FROM_SYSTEM = (
    "dc0d3360fd0ed0750f19bdc7fb8e98b7d3e5aeb10236baa568fa09eceab5c5a4"
)
_EXPECTED_TR_SYNC_STATE_FROM_SYSTEM = (
    "7284593c11289cad17235c5c5e0334d59bba178188e2d8b86415415e525a6843"
)
_EXPECTED_TR_SYSTEM_SYNC_LOOP = (
    "d21c063226b22948bd34ccaf86453472a53f842821f198f7582e831b269ef0b0"
)
_EXPECTED_TR_UPDATE_SOUND_CONFIG = (
    "17c5b751c9eb4f0e08544167c70d1f452c4fe9a33bc5c2ba3dd53b84fcbad17c"
)
_EXPECTED_TR_INSTALL_VOLUME_BRIDGE = (
    "2c3afda093d9077d07c064a228098b428732a358bb68fc2b858559488999b833"
)
_EXPECTED_MEDIA_PLAYER_INIT = (
    "bcd8a03dc7ca17f067b57bcb1e97aa26d7a4c6f6db64abcf844eeb1e151ee1f4"
)
_EXPECTED_MEDIA_PLAYER_HANDLE_MESSAGE = (
    "48f2c2bbd5e6f2cb510d5e57adffa8cea1babe309310332bac8a32f1f262f1af"
)
_EXPECTED_MEDIA_PLAYER_APPLY_VOLUME = (
    "430a758d1656600082c555fcce1c5d6ab060287526e4d9a45435438b2e358435"
)
_EXPECTED_MEDIA_PLAYER_APPLY_VOLUME_FROM_STATE = (
    "9db2fe3d3da3a3a3dd6a49badbed27f371a4c43a74c48739d6b6a49bc916ef40"
)
_EXPECTED_MEDIA_PLAYER_SET_VOLUME_CALLBACK = (
    "7246bd08ef78115d7a19cd3be227c00e957f76855791e5653c028e32b111ea39"
)
_EXPECTED_MEDIA_PLAYER_GET_STATE = (
    "523a738af8686639c39ec1912597fd579090ab9f406103917c67c3d5547024eb"
)
_EXPECTED_MEDIA_PLAYER_UPDATE_STATE = (
    "6400fc814f8299849da6ee5cdde052225aa60f6150561a4a81bf3b53f03f7e30"
)
_EXPECTED_SERVER_STATE_PERSIST_VOLUME = (
    "ac99e6b8b49b1fdfa922c64e6d70ee46c13b3e204dc88971fb47592647a5e6ea"
)
_EXPECTED_BASE_INIT = "1c8edd949cc12268f15e2ead3af5d9c8125b9c22a9c74f5e7dc5a6695a3eff25"
_EXPECTED_MAIN_MODULE_FILE = (
    "38fe14a2068eaa0bbd4af989ddc1a8581d193edcd98f1fe9a837300bec48648d"
)
_MAX_VENDOR_MODULE_BYTES = 4 * 1024 * 1024
_EXPECTED_SYSTEM_VOLUME_POLL_INTERVAL = 0.5
_DIRECT_SYSTEM_VOLUME_POLL_INTERVAL = 0.05

_VENDOR_BASE_INIT = VoiceSatelliteProtocol.__init__
_VENDOR_BASE_HANDLE_AUDIO = VoiceSatelliteProtocol.handle_audio
_VENDOR_BASE_STOP = VoiceSatelliteProtocol.stop
_VENDOR_BASE_HANDLE_MESSAGE = VoiceSatelliteProtocol.handle_message
_VENDOR_TR_INIT = thirdreality_satellite.TRSatelliteProtocol.__init__
_VENDOR_TR_HANDLE_MESSAGE = thirdreality_satellite.TRSatelliteProtocol.handle_message
_VENDOR_TR_SYNC_VOLUME = (
    thirdreality_satellite.TRSatelliteProtocol._sync_volume_to_system  # noqa: SLF001
)
_VENDOR_TR_SYNC_VOLUME_FROM_SYSTEM = (
    thirdreality_satellite.TRSatelliteProtocol._sync_volume_from_system  # noqa: SLF001
)
_VENDOR_TR_SYNC_STATE_FROM_SYSTEM = (
    thirdreality_satellite.TRSatelliteProtocol._sync_state_from_system  # noqa: SLF001
)
_VENDOR_TR_SYSTEM_SYNC_LOOP = (
    thirdreality_satellite.TRSatelliteProtocol._system_sync_loop  # noqa: SLF001
)
_VENDOR_TR_UPDATE_SOUND_CONFIG = (
    thirdreality_satellite.TRSatelliteProtocol._update_sound_config  # noqa: SLF001
)
_VENDOR_TR_INSTALL_VOLUME_BRIDGE = (
    thirdreality_satellite.TRSatelliteProtocol._install_volume_bridge  # noqa: SLF001
)
_VENDOR_MEDIA_PLAYER_INIT = MediaPlayerEntity.__init__
_VENDOR_MEDIA_PLAYER_HANDLE_MESSAGE = MediaPlayerEntity.handle_message
_VENDOR_MEDIA_PLAYER_APPLY_VOLUME = MediaPlayerEntity._apply_volume  # noqa: SLF001
_VENDOR_MEDIA_PLAYER_APPLY_VOLUME_FROM_STATE = MediaPlayerEntity.apply_volume_from_state
_VENDOR_MEDIA_PLAYER_SET_VOLUME_CALLBACK = MediaPlayerEntity.set_volume_callback
_VENDOR_MEDIA_PLAYER_GET_STATE = MediaPlayerEntity._get_state_message  # noqa: SLF001
_VENDOR_MEDIA_PLAYER_UPDATE_STATE = MediaPlayerEntity._update_state  # noqa: SLF001
_VENDOR_SERVER_STATE_PERSIST_VOLUME = ServerState.persist_volume
_REALTIME_SUPPORT: Any = None
_REALTIME_CONFIG: Any = None
_REALTIME_PATCH_ACTIVE = False
_REALTIME_OWNER_ATTRIBUTE = "_codex_realtime_owner"
_REALTIME_PREROLL_ATTRIBUTE = "_codex_realtime_preroll"
_REALTIME_LOCK_ATTRIBUTE = "_codex_realtime_lock"
_REALTIME_STOP_REQUESTED_ATTRIBUTE = "_codex_realtime_stop_requested"
_REALTIME_SOUND_SIGNATURE_ATTRIBUTE = "_codex_realtime_sound_signature"
_REALTIME_ANCHOR_DIRTY_ATTRIBUTE = "_codex_realtime_anchor_dirty"
# The pinned recorder emits 2,048-byte PCM16 frames every 64 ms. Retain six
# idle frames only for the bridge-PCM path; device WebRTC deliberately drops
# all pre-cue PCM and opens live capture after its audible ready boundary.
_REALTIME_PREROLL_MAX_BYTES = 12 * 1024
# Preserve one second of bridge-PCM capacity behind pre-roll so legal, smaller
# custom queues do not fall back merely because their handshake is pending.
_REALTIME_STARTUP_HEADROOM_BYTES = 32 * 1024
_DIRECT_STARTUP_MAX_ATTEMPTS = 3
_DIRECT_STARTUP_DEADLINE_SECONDS = 12.0
_DIRECT_READY_CUE_TIMEOUT_SECONDS = 2.0
_REALTIME_LOCK_CREATION = threading.Lock()
_DIRECT_VOLUME_LIVE_SESSION_STATES = frozenset({"CONNECTING", "READY", "INTERRUPTING"})
_DIRECT_VOLUME_STARTUP_SESSION_STATES = _DIRECT_VOLUME_LIVE_SESSION_STATES | {"NEW"}
_SOUND_CONFIG_LOCK_PATH = "/tmp/sound_config.lock"  # noqa: S108 - vendor ABI
_SOUND_CONFIG_LOCK_TIMEOUT_SECONDS = 0.250
_SOUND_CONFIG_LOCK_RETRY_SECONDS = 0.005
_SOUND_CONFIG_MAX_BYTES = 64 * 1024

_LED_TIMEOUT_SECONDS = 2.0
_LED_THREAD_PREFIX = "thirdreality-led"
_LED_MAX_PENDING = 8
_LED_CONDITION = threading.Condition()
_LED_QUEUE: deque[tuple[str, str, list[str]]] = deque()
_LED_WORKER: threading.Thread | None = None
_LED_SHUT_DOWN = False


class _DirectVolumeRequestStatus(Enum):
    """Outcome of one software-volume request at its ownership boundary."""

    APPLIED = auto()
    FAILED = auto()
    OWNER_LOST = auto()


def _code_hash(function: Any) -> str:
    """Return a stable hash for one installed Python code object."""
    return hashlib.sha256(marshal.dumps(function.__code__)).hexdigest()


def _module_file_hash(module_name: str) -> str | None:
    """Hash one bounded installed module file without importing its code."""
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, AttributeError, ValueError):
        return None
    origin = getattr(spec, "origin", None)
    if not isinstance(origin, str) or not origin.startswith("/"):
        return None
    try:
        with open(origin, "rb") as handle:  # noqa: PTH123 - guarded vendor path
            content = handle.read(_MAX_VENDOR_MODULE_BYTES + 1)
    except OSError:
        return None
    if len(content) > _MAX_VENDOR_MODULE_BYTES:
        return None
    return hashlib.sha256(content).hexdigest()


class _RealtimeOwner:
    """Synchronized vendor state around a vendor-agnostic session."""

    __slots__ = (
        "capture_open",
        "ducked",
        "fallback_audio",
        "fallback_bytes",
        "ready_confirmation_deadline",
        "ready_confirmation_pending",
        "ready_seen",
        "released",
        "session",
        "startup_attempt",
        "startup_deadline",
        "stop_requested",
        "stop_word_id",
        "stop_word_was_active",
        "wake_word",
        "watch_generation",
    )

    def __init__(
        self,
        *,
        session: Any,
        wake_word: Any,
        stop_word_id: Any,
        stop_word_was_active: bool,
    ) -> None:
        self.session = session
        self.wake_word = wake_word
        self.stop_word_id = stop_word_id
        self.stop_word_was_active = stop_word_was_active
        self.ducked = False
        self.capture_open = False
        self.ready_confirmation_deadline: float | None = None
        self.ready_confirmation_pending = False
        self.ready_seen = False
        self.released = False
        self.startup_attempt = 1
        self.startup_deadline: float | None = None
        self.watch_generation = 0
        self.stop_requested = False
        self.fallback_bytes = 0
        self.fallback_audio: deque[bytes] = deque()


def _load_realtime_config() -> None:
    """Publish the exact configuration snapshot used for capture selection."""
    global _REALTIME_CONFIG, _REALTIME_SUPPORT  # noqa: PLW0603
    if _EARLY_REALTIME_CONFIG is None:
        return
    _REALTIME_SUPPORT = _EARLY_REALTIME_SUPPORT
    _REALTIME_CONFIG = _EARLY_REALTIME_CONFIG


def _classify_wake(wake_word: Any) -> tuple[bool, str]:
    if (
        not _REALTIME_PATCH_ACTIVE
        or _REALTIME_CONFIG is None
        or _REALTIME_SUPPORT is None
    ):
        return False, "realtime_unavailable"
    phrase = getattr(wake_word, "wake_word", "")
    if not isinstance(phrase, str):
        return False, "invalid_phrase"
    if _REALTIME_SUPPORT.normalize_wake_phrase(phrase) == _REALTIME_CONFIG.wake_phrase:
        return True, "configured_phrase"
    if _realtime_only_mode():
        return False, "realtime_only"
    return False, "normal_phrase"


def _is_realtime_wake(wake_word: Any) -> bool:
    return _classify_wake(wake_word)[0]


def _log_wake_selection(detector: str, selection: str) -> None:
    """Emit one fixed-vocabulary detector event to the device system log."""
    if detector not in {"realtime", "assist", "disabled"}:
        return
    if selection not in {
        "configured_phrase",
        "invalid_phrase",
        "normal_phrase",
        "realtime_only",
        "realtime_unavailable",
    }:
        return
    with suppress(Exception):  # diagnostics cannot affect wake routing
        syslog.syslog(
            syslog.LOG_INFO,
            f"codex-voice wake_detector={detector} selection={selection}",
        )


def _tune_realtime_wake(wake_word: Any) -> None:
    """Apply an optional validated cutoff to only the configured detector."""
    cutoff = getattr(_REALTIME_CONFIG, "wake_probability_cutoff", None)
    if cutoff is None:
        return
    observed = getattr(wake_word, "probability_cutoff", None)
    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        return
    with suppress(Exception):  # keep the vendor detector unchanged
        wake_word.probability_cutoff = cutoff


class _RealtimeFirstWakeWords(dict[Any, Any]):
    """Keep vendor mapping semantics but expose direct detectors first."""

    def values(self) -> tuple[Any, ...]:
        observed = tuple(dict.values(self))
        realtime: list[Any] = []
        normal: list[Any] = []
        for wake_word in observed:
            if _is_realtime_wake(wake_word):
                _tune_realtime_wake(wake_word)
                realtime.append(wake_word)
            else:
                normal.append(wake_word)
        return (*realtime, *normal)


def _install_realtime_wake_order(state: Any) -> None:
    """Replace one existing vendor mapping without mutating it in place."""
    wake_words = getattr(state, "wake_words", None)
    if isinstance(wake_words, dict) and not isinstance(
        wake_words, _RealtimeFirstWakeWords
    ):
        state.wake_words = _RealtimeFirstWakeWords(wake_words)


def _fast_thirdreality_init(instance: Any, state: Any = None) -> None:
    """Install deterministic wake arbitration before publishing the protocol."""
    if state is None:
        # Compatibility for constructor shims that manufacture their own state.
        # The guarded appliance constructor always supplies state and therefore
        # takes the pre-publication branch below.
        _VENDOR_TR_INIT(instance)
        _install_realtime_wake_order(instance.state)
        setattr(instance, _REALTIME_ANCHOR_DIRTY_ATTRIBUTE, False)
        _remember_sound_config_signature(instance)
        return
    _install_realtime_wake_order(state)
    _VENDOR_TR_INIT(instance, state)
    setattr(instance, _REALTIME_ANCHOR_DIRTY_ATTRIBUTE, False)
    _remember_sound_config_signature(instance)


def _uses_device_webrtc() -> bool:
    """Return whether the configured wake owns provider media on this device."""
    if _REALTIME_CONFIG is None or _REALTIME_SUPPORT is None:
        return False
    return getattr(_REALTIME_CONFIG, "media_transport", None) == getattr(
        _REALTIME_SUPPORT, "DEVICE_WEBRTC_TRANSPORT", "device_webrtc"
    )


def _uses_deterministic_realtime_media() -> bool:
    """Return whether capture starts only after one confirmed live session."""
    if _REALTIME_CONFIG is None or _REALTIME_SUPPORT is None:
        return False
    if not bool(getattr(_REALTIME_CONFIG, "full_duplex", False)):
        return False
    media_transport = getattr(_REALTIME_CONFIG, "media_transport", None)
    return media_transport in {
        getattr(_REALTIME_SUPPORT, "BRIDGE_PCM_TRANSPORT", "bridge_pcm"),
        getattr(_REALTIME_SUPPORT, "DEVICE_WEBRTC_TRANSPORT", "device_webrtc"),
    }


def _realtime_only_mode() -> bool:
    """Return whether this appliance must never enter the Assist wake path."""
    return bool(getattr(_REALTIME_CONFIG, "realtime_only", False))


def _assist_fallback_allowed() -> bool:
    """Return whether buffered direct audio may fall back to Home Assistant."""
    return not _uses_deterministic_realtime_media() and not _realtime_only_mode()


def _configure_stop_word_membership(instance: Any) -> tuple[Any, bool]:
    """Select the vendor stop detector behavior for one direct session."""
    stop_word = getattr(instance.state, "stop_word", None)
    stop_word_id = getattr(stop_word, "id", None)
    active = getattr(instance.state, "active_wake_words", None)
    if stop_word_id is None or active is None:
        return None, False
    was_active = stop_word_id in active
    if _uses_deterministic_realtime_media():
        # The direct conversation owns the microphone from wake acceptance
        # through terminal teardown. Keeping the vendor stop model armed here
        # can reinterpret the wake tail as a stop and cancel negotiation before
        # the sidecar even creates its offer. Direct mode therefore exposes the
        # explicit end-conversation tool as its sole terminal voice control.
        active.discard(stop_word_id)
    elif not was_active:
        # Half-duplex bridge PCM has no realtime barge-in while output is gated,
        # so preserve its explicit terminal stop control.
        active.add(stop_word_id)
    return stop_word_id, was_active


def _suspend_live_stop_word(instance: Any, owner: _RealtimeOwner) -> None:
    """Move terminal control from the local detector to realtime speech."""
    if owner.stop_word_id is None:
        return
    active = getattr(instance.state, "active_wake_words", None)
    if active is not None:
        active.discard(owner.stop_word_id)


def _restore_stop_word_membership(instance: Any, owner: _RealtimeOwner) -> None:
    if owner.stop_word_id is None:
        return
    active = getattr(instance.state, "active_wake_words", None)
    if active is None:
        return
    if owner.stop_word_was_active:
        active.add(owner.stop_word_id)
    else:
        active.discard(owner.stop_word_id)


def _realtime_state_lock(instance: Any) -> Any:
    """Return the per-protocol reentrant lock, creating it exactly once."""
    lock = getattr(instance, _REALTIME_LOCK_ATTRIBUTE, None)
    if lock is not None:
        return lock
    with _REALTIME_LOCK_CREATION:
        lock = getattr(instance, _REALTIME_LOCK_ATTRIBUTE, None)
        if lock is None:
            lock = threading.RLock()
            setattr(instance, _REALTIME_LOCK_ATTRIBUTE, lock)
    return lock


def _remember_realtime_preroll(instance: Any, audio_chunk: bytes) -> None:
    """Retain only the newest bounded idle PCM for delayed wake activation."""
    preroll = getattr(instance, _REALTIME_PREROLL_ATTRIBUTE, None)
    if not isinstance(preroll, deque):
        preroll = deque()
        setattr(instance, _REALTIME_PREROLL_ATTRIBUTE, preroll)

    retained = bytes(audio_chunk)
    if len(retained) > _REALTIME_PREROLL_MAX_BYTES:
        retained = retained[-_REALTIME_PREROLL_MAX_BYTES:]
    if retained:
        preroll.append(retained)

    retained_bytes = sum(len(chunk) for chunk in preroll)
    while preroll and retained_bytes > _REALTIME_PREROLL_MAX_BYTES:
        retained_bytes -= len(preroll.popleft())


def _take_realtime_preroll(instance: Any) -> list[bytes]:
    """Atomically detach idle pre-roll on the pinned microphone thread."""
    preroll = getattr(instance, _REALTIME_PREROLL_ATTRIBUTE, None)
    setattr(instance, _REALTIME_PREROLL_ATTRIBUTE, None)
    if not isinstance(preroll, deque):
        return []
    return list(preroll)


def _discard_realtime_preroll(instance: Any) -> None:
    """Forget idle PCM without forwarding it to either backend."""
    setattr(instance, _REALTIME_PREROLL_ATTRIBUTE, None)


def _preroll_with_startup_headroom(preroll_audio: list[bytes]) -> list[bytes]:
    """Keep newest PCM without consuming the live cold-start allowance."""
    fallback_capacity = _REALTIME_CONFIG.fallback_buffer_bytes
    input_capacity = getattr(
        _REALTIME_CONFIG,
        "input_queue_bytes",
        fallback_capacity,
    )
    # Device-owned WebRTC can never replay capture into Home Assistant, so its
    # compatibility fallback deque is intentionally empty. Do not let that
    # unused bound suppress valid direct pre-roll; only the real input queue
    # and its reserved live headroom constrain direct startup.
    startup_capacity = (
        min(fallback_capacity, input_capacity)
        if _assist_fallback_allowed()
        else input_capacity
    )
    budget = min(
        _REALTIME_PREROLL_MAX_BYTES,
        max(
            0,
            startup_capacity - _REALTIME_STARTUP_HEADROOM_BYTES,
        ),
    )
    if budget == 0:
        return []

    selected: deque[bytes] = deque()
    remaining = budget
    for chunk in reversed(preroll_audio):
        if len(chunk) <= remaining:
            selected.appendleft(chunk)
            remaining -= len(chunk)
        else:
            aligned_remaining = remaining - (remaining % 2)
            if aligned_remaining:
                selected.appendleft(chunk[-aligned_remaining:])
            break
        if remaining == 0:
            break
    return list(selected)


def _construct_realtime_session(
    maximum_attempts: int,
    *,
    deadline: float | None,
) -> tuple[Any | None, int]:
    """Construct one session under a bounded direct-start retry budget."""
    for attempt in range(1, maximum_attempts + 1):
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            return _REALTIME_SUPPORT.RealtimeSession(_REALTIME_CONFIG), attempt
        except Exception:  # noqa: BLE001 - optional client must fail closed
            continue
    return None, maximum_attempts


def _prepare_realtime_startup_audio(
    preroll_audio: list[bytes],
) -> tuple[list[bytes], int]:
    """Apply the transport-specific capture boundary and retry policy."""
    if _uses_deterministic_realtime_media():
        return [], _DIRECT_STARTUP_MAX_ATTEMPTS
    return _preroll_with_startup_headroom(preroll_audio), 1


def _activate_realtime_owner(
    instance: Any,
    owner: _RealtimeOwner,
    preroll_audio: list[bytes],
) -> None:
    """Start transport ownership and apply the one-time vendor duck."""
    try:
        _initialize_direct_session_volume(instance, owner)
        owner.session.start()
        if (
            owner.startup_deadline is not None
            and time.monotonic() >= owner.startup_deadline
        ):
            _fallback_realtime_to_ha(instance, owner)
            return
    except Exception:  # noqa: BLE001 - optional client must fail closed
        if not _retry_direct_realtime_startup(
            instance,
            owner,
            start_watcher=False,
        ):
            _fallback_realtime_to_ha(instance, owner)
            return
    try:
        if owner.stop_requested:
            _interrupt_realtime_owner(instance, owner)
            return
        if _uses_deterministic_realtime_media() and not _start_direct_lifecycle_watcher(
            instance,
            owner,
        ):
            _fallback_realtime_to_ha(instance, owner)
            return
        # Mark the side effect as attempted before calling vendor code so a
        # partial duck that raises is still undone during rollback.
        owner.ducked = True
        instance.duck()
        if owner.stop_requested:
            _interrupt_realtime_owner(instance, owner)
            return
        for chunk in preroll_audio:
            result = owner.session.submit_audio(chunk)
            if owner.stop_requested:
                _interrupt_realtime_owner(instance, owner)
                return
            if result not in {
                _REALTIME_SUPPORT.SubmitResult.ACCEPTED,
                _REALTIME_SUPPORT.SubmitResult.GATED,
            }:
                _fallback_realtime_to_ha(instance, owner)
                return
    except Exception:  # noqa: BLE001 - vendor calls have no stable exception API
        if owner.stop_requested:
            _interrupt_realtime_owner(instance, owner)
        else:
            _fallback_realtime_to_ha(instance, owner)


def _start_realtime_wakeup(
    instance: Any,
    wake_word: Any,
    preroll_audio: list[bytes],
) -> None:
    """Claim audio for a fresh direct session without touching HA networking."""
    with _realtime_state_lock(instance):
        if instance._timer_finished:  # noqa: SLF001
            _fast_wakeup(instance, wake_word)
            return
        if instance.state.muted or not instance.state.connected:
            return
        if instance._pipeline_active:  # noqa: SLF001
            _LOGGER.debug("Ignoring wake word - pipeline already active")
            return

        # A direct WebRTC wake is only permission to establish a session. The
        # triggering audio and every frame recorded while signaling are stale
        # by the time RTP starts, so never enqueue them. The ready cue below is
        # the single, audible boundary after which capture becomes live.
        preroll_audio, maximum_attempts = _prepare_realtime_startup_audio(preroll_audio)
        startup_deadline = (
            time.monotonic() + _DIRECT_STARTUP_DEADLINE_SECONDS
            if _uses_deterministic_realtime_media()
            else None
        )
        if _uses_deterministic_realtime_media():
            _nonblocking_led_fire("thinking")
        session, startup_attempt = _construct_realtime_session(
            maximum_attempts,
            deadline=startup_deadline,
        )
        if session is None:
            if getattr(instance, _REALTIME_STOP_REQUESTED_ATTRIBUTE, False):
                if _uses_deterministic_realtime_media():
                    _nonblocking_led_fire("idle", to_idle=True)
                return
            if _assist_fallback_allowed():
                _LOGGER.warning(
                    "ThirdReality realtime startup fell back to Home Assistant"
                )
                _fast_wakeup(instance, wake_word)
                if _wake_is_armed(instance):
                    for chunk in preroll_audio:
                        _VENDOR_BASE_HANDLE_AUDIO(instance, chunk)
            else:
                _LOGGER.warning("ThirdReality realtime startup failed closed")
                _nonblocking_led_fire("idle", to_idle=True)
            return

        if getattr(instance, _REALTIME_STOP_REQUESTED_ATTRIBUTE, False):
            try:
                session.stop()
            except Exception:  # noqa: BLE001 - best-effort unowned cleanup
                _LOGGER.warning("Failed to stop unowned ThirdReality session")
            if _uses_deterministic_realtime_media():
                _nonblocking_led_fire("idle", to_idle=True)
            return
        try:
            owner = _RealtimeOwner(
                session=session,
                wake_word=wake_word,
                stop_word_id=None,
                stop_word_was_active=False,
            )
            owner.startup_attempt = startup_attempt
            owner.startup_deadline = startup_deadline
            stop_word_id, stop_word_was_active = _configure_stop_word_membership(
                instance
            )
            owner.stop_word_id = stop_word_id
            owner.stop_word_was_active = stop_word_was_active
        except Exception:  # noqa: BLE001 - preserve the normal wake path
            try:
                session.stop()
            except Exception:  # noqa: BLE001 - best-effort unowned cleanup
                _LOGGER.warning("Failed to stop unowned ThirdReality session")
            if (
                not getattr(instance, _REALTIME_STOP_REQUESTED_ATTRIBUTE, False)
                and _assist_fallback_allowed()
            ):
                _LOGGER.warning(
                    "ThirdReality realtime startup fell back to Home Assistant"
                )
                _fast_wakeup(instance, wake_word)
                if _wake_is_armed(instance):
                    for chunk in preroll_audio:
                        _VENDOR_BASE_HANDLE_AUDIO(instance, chunk)
            elif not getattr(instance, _REALTIME_STOP_REQUESTED_ATTRIBUTE, False):
                _LOGGER.warning("ThirdReality realtime startup failed closed")
                _nonblocking_led_fire("idle", to_idle=True)
            return
        if _assist_fallback_allowed():
            owner.fallback_audio.extend(preroll_audio)
            owner.fallback_bytes = sum(len(chunk) for chunk in preroll_audio)
        setattr(instance, _REALTIME_OWNER_ATTRIBUTE, owner)
        # The pinned capture loop already uses these flags to decide whether to
        # call handle_audio and suppress duplicate wakes. The guarded wrapper
        # owns every frame until this synchronized owner is detached.
        instance._pipeline_active = True  # noqa: SLF001
        instance._is_streaming_audio = True  # noqa: SLF001
        _activate_realtime_owner(instance, owner, preroll_audio)


def _retry_direct_realtime_startup(
    instance: Any,
    owner: _RealtimeOwner,
    *,
    start_watcher: bool = True,
) -> bool:
    """Replace a failed pre-ready session without releasing device state."""
    if (
        not _uses_deterministic_realtime_media()
        or owner.stop_requested
        or owner.released
        or owner.ready_seen
        or owner.startup_attempt >= _DIRECT_STARTUP_MAX_ATTEMPTS
    ):
        return False
    if getattr(instance, _REALTIME_OWNER_ATTRIBUTE, None) is not owner:
        return False
    if (
        owner.startup_deadline is not None
        and time.monotonic() >= owner.startup_deadline
    ):
        return False

    try:
        owner.session.stop()
    except Exception:  # noqa: BLE001 - a failed child is disposable
        _LOGGER.warning("Failed to stop a rejected ThirdReality startup attempt")

    while owner.startup_attempt < _DIRECT_STARTUP_MAX_ATTEMPTS:
        if (
            owner.startup_deadline is not None
            and time.monotonic() >= owner.startup_deadline
        ):
            break
        owner.startup_attempt += 1
        try:
            replacement = _REALTIME_SUPPORT.RealtimeSession(_REALTIME_CONFIG)
            owner.session = replacement
            owner.ready_seen = False
            owner.capture_open = False
            owner.ready_confirmation_pending = False
            owner.ready_confirmation_deadline = None
            _initialize_direct_session_volume(instance, owner)
            replacement.start()
            if (
                owner.startup_deadline is not None
                and time.monotonic() >= owner.startup_deadline
            ):
                replacement.stop()
                break
        except Exception:  # noqa: BLE001 - exhaust the bounded retry policy
            with suppress(Exception):
                owner.session.stop()
            continue
        with suppress(Exception):
            syslog.syslog(
                syslog.LOG_INFO,
                "codex-voice realtime_startup_retry "
                f"attempt={owner.startup_attempt}/{_DIRECT_STARTUP_MAX_ATTEMPTS}",
            )
        if start_watcher and not _start_direct_lifecycle_watcher(instance, owner):
            return False
        return True
    return False


def _start_direct_lifecycle_watcher(
    instance: Any,
    owner: _RealtimeOwner,
) -> bool:
    """Observe direct startup independently of microphone callback progress."""
    owner.watch_generation += 1
    generation = owner.watch_generation
    watched_session = owner.session
    watcher = threading.Thread(
        target=_watch_direct_lifecycle,
        args=(instance, owner, watched_session, generation),
        name="thirdreality-realtime-startup",
        daemon=True,
    )
    try:
        watcher.start()
    except Exception:  # noqa: BLE001 - a missing watcher violates startup safety
        _LOGGER.warning("ThirdReality realtime startup watcher failed")
        return False
    return True


def _watch_direct_lifecycle(
    instance: Any,
    owner: _RealtimeOwner,
    watched_session: Any,
    generation: int,
) -> None:
    """Drive READY, retry, deadline, and terminal transitions for one attempt."""
    while True:
        time.sleep(0.02)
        with _realtime_state_lock(instance):
            if owner.released or owner.stop_requested:
                return
            if getattr(instance, _REALTIME_OWNER_ATTRIBUTE, None) is not owner:
                return
            if (
                generation != owner.watch_generation
                or owner.session is not watched_session
            ):
                return
            startup_deadline = owner.startup_deadline
            if owner.ready_seen:
                if watched_session.terminal:
                    _detach_realtime_owner(instance, owner, unduck=owner.ducked)
                    return
                cue_deadline = owner.ready_confirmation_deadline
                if (
                    owner.ready_confirmation_pending
                    and cue_deadline is not None
                    and time.monotonic() >= cue_deadline
                ):
                    _LOGGER.warning("ThirdReality realtime ready cue timed out")
                    _interrupt_realtime_owner(instance, owner)
                    return
                if owner.capture_open:
                    continue
                if not owner.ready_confirmation_pending:
                    _LOGGER.warning(
                        "ThirdReality realtime capture remained closed after ready"
                    )
                    _interrupt_realtime_owner(instance, owner)
                    return
                continue
            ready_at = getattr(watched_session, "ready_at", None)
            ready = bool(watched_session.ready)
            if ready:
                if (
                    startup_deadline is None
                    or not isinstance(ready_at, (int, float))
                    or isinstance(ready_at, bool)
                    or ready_at >= startup_deadline
                ):
                    _LOGGER.warning("ThirdReality realtime startup deadline expired")
                    _fallback_realtime_to_ha(instance, owner)
                    return
                owner.ready_seen = True
                owner.startup_deadline = None
                _begin_direct_ready_confirmation(instance, owner)
                continue
            if startup_deadline is not None and time.monotonic() >= startup_deadline:
                _LOGGER.warning("ThirdReality realtime startup deadline expired")
                _fallback_realtime_to_ha(instance, owner)
                return
            if watched_session.failed_before_ready:
                if not _retry_direct_realtime_startup(instance, owner):
                    _fallback_realtime_to_ha(instance, owner)
                return
            if watched_session.terminal:
                _detach_realtime_owner(instance, owner, unduck=owner.ducked)
                return


def _complete_direct_ready_confirmation(
    instance: Any,
    owner: _RealtimeOwner,
) -> None:
    """Open live capture only after the native ready cue reaches EOF."""
    with _realtime_state_lock(instance):
        if owner.released or owner.stop_requested:
            return
        if getattr(instance, _REALTIME_OWNER_ATTRIBUTE, None) is not owner:
            return
        if not owner.ready_confirmation_pending:
            return
        if owner.session.terminal or not owner.session.ready:
            return
        deadline = owner.ready_confirmation_deadline
        if deadline is None or time.monotonic() >= deadline:
            _LOGGER.warning("ThirdReality realtime ready cue timed out")
            _interrupt_realtime_owner(instance, owner)
            return
        owner.ready_confirmation_pending = False
        owner.ready_confirmation_deadline = None
        owner.startup_deadline = None
        _suspend_live_stop_word(instance, owner)
        owner.capture_open = True
        _nonblocking_led_fire("listening")
        owner.session.notify_live_capture_opened()
        with suppress(Exception):
            syslog.syslog(
                syslog.LOG_INFO,
                "codex-voice realtime_capture_open "
                f"attempt={owner.startup_attempt}/{_DIRECT_STARTUP_MAX_ATTEMPTS}",
            )


def _begin_direct_ready_confirmation(instance: Any, owner: _RealtimeOwner) -> None:
    """Play exactly one session-ready cue while capture remains closed."""
    if owner.ready_confirmation_pending or owner.capture_open:
        return
    owner.ready_confirmation_pending = True
    owner.ready_confirmation_deadline = (
        time.monotonic() + _DIRECT_READY_CUE_TIMEOUT_SECONDS
    )

    def _on_ready_cue_finished() -> None:
        _complete_direct_ready_confirmation(instance, owner)

    try:
        instance.state.tts_player.play(
            instance.state.wakeup_sound,
            done_callback=_on_ready_cue_finished,
        )
    except Exception:  # noqa: BLE001 - capture must not open without the cue
        owner.ready_confirmation_pending = False
        owner.ready_confirmation_deadline = None
        _LOGGER.warning("ThirdReality realtime ready cue failed")
        _interrupt_realtime_owner(instance, owner)


def _detach_realtime_owner(
    instance: Any,
    owner: _RealtimeOwner,
    *,
    unduck: bool,
) -> None:
    """Idempotently release only state added for this direct session."""
    with _realtime_state_lock(instance):
        if owner.released:
            return
        if getattr(instance, _REALTIME_OWNER_ATTRIBUTE, None) is not owner:
            return
        cue_was_pending = owner.ready_confirmation_pending
        owner.released = True
        owner.ready_confirmation_pending = False
        owner.ready_confirmation_deadline = None
        owner.startup_deadline = None
        owner.capture_open = False
        setattr(instance, _REALTIME_OWNER_ATTRIBUTE, None)
        _discard_realtime_preroll(instance)
        instance._is_streaming_audio = False  # noqa: SLF001
        instance._pipeline_active = False  # noqa: SLF001
        _restore_stop_word_membership(instance, owner)
        owner.fallback_audio.clear()
        owner.fallback_bytes = 0
        should_unduck = unduck and owner.ducked
        owner.ducked = False
        if should_unduck:
            try:
                instance.unduck()
            except Exception:  # noqa: BLE001 - best-effort vendor cleanup
                _LOGGER.warning("Failed to unduck after ThirdReality realtime session")
        if cue_was_pending:
            try:
                instance.state.tts_player.stop()
            except Exception:  # noqa: BLE001 - owner is already safely released
                _LOGGER.warning("Failed to stop ThirdReality realtime ready cue")
        _nonblocking_led_fire("idle", to_idle=True)


def _reconcile_realtime_owner(instance: Any) -> _RealtimeOwner | None:
    """Release a terminal owner and return only a still-live owner."""
    with _realtime_state_lock(instance):
        owner = getattr(instance, _REALTIME_OWNER_ATTRIBUTE, None)
        if owner is None or not owner.session.terminal:
            return owner
        _detach_realtime_owner(instance, owner, unduck=owner.ducked)
        return getattr(instance, _REALTIME_OWNER_ATTRIBUTE, None)


def _interrupt_realtime_owner(
    instance: Any,
    owner: _RealtimeOwner,
    *,
    source: str = "internal",
) -> None:
    """Interrupt and release one current owner without starting HA fallback."""
    with _realtime_state_lock(instance):
        if owner.released:
            return
        if getattr(instance, _REALTIME_OWNER_ATTRIBUTE, None) is not owner:
            return
        safe_source = (
            source
            if source
            in {
                "audio_submit_exception",
                "audio_submit_rejected",
                "internal",
                "mute",
                "vendor_stop",
                "volume_command",
                "volume_sync",
            }
            else "internal"
        )
        with suppress(Exception):
            syslog.syslog(
                syslog.LOG_INFO,
                "codex-voice realtime_interrupt "
                f"source={safe_source} "
                f"phase={'live' if owner.capture_open else 'startup'}",
            )
        try:
            # Vendor teardown releases the microphone owner immediately. Even
            # when the bridge confirms provider cancellation, this session
            # must therefore close instead of resuming without an owner.
            owner.session.interrupt(preserve_session=False)
        except Exception:  # noqa: BLE001 - cleanup must still release vendor state
            _LOGGER.warning("Failed to interrupt ThirdReality realtime session")
        finally:
            _detach_realtime_owner(instance, owner, unduck=owner.ducked)


def _begin_realtime_fallback_handoff(instance: Any, owner: _RealtimeOwner) -> bool:
    """Release direct state but retain a stop-visible HA handoff marker."""
    if owner.released:
        return False
    if getattr(instance, _REALTIME_OWNER_ATTRIBUTE, None) is not owner:
        return False
    _discard_realtime_preroll(instance)
    instance._is_streaming_audio = False  # noqa: SLF001
    instance._pipeline_active = False  # noqa: SLF001
    _restore_stop_word_membership(instance, owner)
    owner.fallback_audio.clear()
    owner.fallback_bytes = 0
    should_unduck = owner.ducked
    owner.ducked = False
    if should_unduck:
        try:
            instance.unduck()
        except Exception:  # noqa: BLE001 - HA fallback must continue safely
            _LOGGER.warning("Failed to unduck before Home Assistant fallback")
    return True


def _finish_realtime_fallback_handoff(instance: Any, owner: _RealtimeOwner) -> None:
    """Detach a completed marker without altering HA-owned pipeline state."""
    if getattr(instance, _REALTIME_OWNER_ATTRIBUTE, None) is owner:
        owner.released = True
        setattr(instance, _REALTIME_OWNER_ATTRIBUTE, None)


def _cancel_realtime_fallback_handoff(instance: Any, owner: _RealtimeOwner) -> None:
    """Explicitly cancel an HA pipeline started during a concurrent stop."""
    if _wake_is_armed(instance):
        phrase = getattr(owner.wake_word, "wake_word", "")
        _rollback_wakeup(
            instance,
            phrase,
            notify_home_assistant=True,
            unduck=True,
        )


def _fallback_realtime_to_ha(
    instance: Any,
    owner: _RealtimeOwner,
    trailing_audio: bytes | None = None,
) -> None:
    """Return startup failure to HA on the vendor microphone thread."""
    with _realtime_state_lock(instance):
        if owner.released or owner.stop_requested:
            return
        if getattr(instance, _REALTIME_OWNER_ATTRIBUTE, None) is not owner:
            return
        replay = list(owner.fallback_audio)
        try:
            owner.session.stop()
        except Exception:  # noqa: BLE001 - HA fallback must survive cleanup errors
            _LOGGER.warning("Failed to stop ThirdReality realtime session")
        if not _assist_fallback_allowed():
            _LOGGER.warning("ThirdReality realtime session failed closed")
            _detach_realtime_owner(instance, owner, unduck=owner.ducked)
            return
        if owner.stop_requested or getattr(
            instance,
            _REALTIME_STOP_REQUESTED_ATTRIBUTE,
            False,
        ):
            _detach_realtime_owner(instance, owner, unduck=owner.ducked)
            return
        if not _begin_realtime_fallback_handoff(instance, owner):
            return
        try:
            if owner.stop_requested or getattr(
                instance,
                _REALTIME_STOP_REQUESTED_ATTRIBUTE,
                False,
            ):
                return
            _LOGGER.warning("ThirdReality realtime startup fell back to Home Assistant")
            _fast_wakeup(instance, owner.wake_word)
            if owner.stop_requested or getattr(
                instance,
                _REALTIME_STOP_REQUESTED_ATTRIBUTE,
                False,
            ):
                _cancel_realtime_fallback_handoff(instance, owner)
                return
            if not _wake_is_armed(instance):
                return
            for chunk in replay:
                if owner.stop_requested or getattr(
                    instance,
                    _REALTIME_STOP_REQUESTED_ATTRIBUTE,
                    False,
                ):
                    _cancel_realtime_fallback_handoff(instance, owner)
                    return
                _VENDOR_BASE_HANDLE_AUDIO(instance, chunk)
            if trailing_audio:
                if owner.stop_requested or getattr(
                    instance,
                    _REALTIME_STOP_REQUESTED_ATTRIBUTE,
                    False,
                ):
                    _cancel_realtime_fallback_handoff(instance, owner)
                    return
                _VENDOR_BASE_HANDLE_AUDIO(instance, trailing_audio)
        finally:
            if owner.stop_requested or getattr(
                instance,
                _REALTIME_STOP_REQUESTED_ATTRIBUTE,
                False,
            ):
                _cancel_realtime_fallback_handoff(instance, owner)
            _finish_realtime_fallback_handoff(instance, owner)


def _realtime_handle_audio(instance: Any, audio_chunk: bytes) -> None:
    """Route mic PCM only while a compatible direct session owns capture."""
    with _realtime_state_lock(instance):
        owner = getattr(instance, _REALTIME_OWNER_ATTRIBUTE, None)
        if owner is None:
            if (
                not instance._pipeline_active  # noqa: SLF001
                and not instance._is_streaming_audio  # noqa: SLF001
                and instance.state.connected
                and not instance.state.muted
            ):
                _remember_realtime_preroll(instance, audio_chunk)
            else:
                _discard_realtime_preroll(instance)
            _VENDOR_BASE_HANDLE_AUDIO(instance, audio_chunk)
            return
        if owner.released or owner.stop_requested:
            return
        if instance.state.muted:
            owner.stop_requested = True
            _interrupt_realtime_owner(instance, owner, source="mute")
            return
        if _uses_deterministic_realtime_media() and not owner.capture_open:
            # The lifecycle watcher exclusively owns CONNECTING/CONFIRMING.
            # This callback is therefore a pure drop boundary until cue EOF.
            return
        if owner.session.failed_before_ready:
            if _retry_direct_realtime_startup(instance, owner):
                return
            _fallback_realtime_to_ha(instance, owner, audio_chunk)
            return
        if owner.session.terminal:
            _detach_realtime_owner(instance, owner, unduck=owner.ducked)
            return

        if not _uses_deterministic_realtime_media() and not owner.session.ready:
            if _assist_fallback_allowed():
                maximum = _REALTIME_CONFIG.fallback_buffer_bytes
                if owner.fallback_bytes + len(audio_chunk) > maximum:
                    _fallback_realtime_to_ha(instance, owner, audio_chunk)
                    return
                owner.fallback_audio.append(audio_chunk)
                owner.fallback_bytes += len(audio_chunk)
        elif not owner.ready_seen:
            owner.ready_seen = True
            owner.fallback_audio.clear()
            owner.fallback_bytes = 0

        try:
            result = owner.session.submit_audio(audio_chunk)
        except Exception:  # noqa: BLE001 - client failure must release capture
            if owner.stop_requested:
                _interrupt_realtime_owner(instance, owner)
            elif not owner.ready_seen:
                _fallback_realtime_to_ha(instance, owner)
            else:
                _interrupt_realtime_owner(
                    instance,
                    owner,
                    source="audio_submit_exception",
                )
            return
        if owner.stop_requested or owner.released:
            return
        if result in {
            _REALTIME_SUPPORT.SubmitResult.ACCEPTED,
            _REALTIME_SUPPORT.SubmitResult.GATED,
        }:
            return
        if not owner.ready_seen:
            # bridge_pcm already retained this frame for HA replay. Direct
            # WebRTC intentionally retains no compatibility copy and closes.
            _fallback_realtime_to_ha(instance, owner)
            return
        _interrupt_realtime_owner(
            instance,
            owner,
            source="audio_submit_rejected",
        )
        _LOGGER.warning("ThirdReality realtime audio session ended safely")


def _direct_volume_owner_is_current(
    instance: Any,
    owner: _RealtimeOwner,
    *,
    allow_new: bool = False,
) -> bool:
    """Linearize software-volume ownership against asynchronous teardown."""
    try:
        session_state_name = owner.session.state.name
    except Exception:  # noqa: BLE001 - malformed optional clients fail closed
        return False
    allowed_states = (
        _DIRECT_VOLUME_STARTUP_SESSION_STATES
        if allow_new
        else _DIRECT_VOLUME_LIVE_SESSION_STATES
    )
    return (
        session_state_name in allowed_states
        and getattr(instance, _REALTIME_OWNER_ATTRIBUTE, None) is owner
        and not owner.released
        and not owner.stop_requested
        and not bool(getattr(owner.session, "terminal", True))
    )


def _live_direct_volume_owner(instance: Any) -> _RealtimeOwner | None:
    """Return only an owner whose output is safe to control in software."""
    if not bool(getattr(_REALTIME_CONFIG, "full_duplex", False)):
        return None
    if not _uses_deterministic_realtime_media():
        return None
    owner = getattr(instance, _REALTIME_OWNER_ATTRIBUTE, None)
    if owner is None or not _direct_volume_owner_is_current(instance, owner):
        return None
    return owner


def _direct_volume_ceiling_percent() -> int | None:
    """Return the configured fixed playback anchor for direct rendering."""
    candidates = (
        getattr(_REALTIME_CONFIG, "aec_sink_volume_ceiling_percent", None),
        getattr(_REALTIME_CONFIG, "playback_volume_percent", None),
    )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100
        for value in candidates
    ):
        return None
    return min(candidates)


def _bounded_direct_volume_percent(value: Any, ceiling: int) -> int | None:
    """Convert one HA normalized volume into a safe integer percentage."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    if not math.isfinite(normalized):
        return None
    return min(round(max(0.0, min(1.0, normalized)) * 100), ceiling)


def _request_direct_volume(
    instance: Any,
    owner: _RealtimeOwner,
    percent: int,
    ceiling: int,
    *,
    allow_new: bool = False,
    reconcile_anchor: bool = False,
) -> tuple[_DirectVolumeRequestStatus, int | None]:
    """Apply software volume and explicitly report its ownership outcome."""
    if not _direct_volume_owner_is_current(instance, owner, allow_new=allow_new):
        return _DirectVolumeRequestStatus.OWNER_LOST, None
    request_name = (
        "reconcile_playback_volume" if reconcile_anchor else "request_playback_volume"
    )
    request = getattr(owner.session, request_name, None)
    if not callable(request):
        _LOGGER.warning("ThirdReality realtime volume control is unavailable")
        return _DirectVolumeRequestStatus.FAILED, None
    try:
        applied = request(percent)
    except RuntimeError:
        _LOGGER.warning("ThirdReality realtime volume control failed")
        # RuntimeError is the public session API's lifecycle rejection. It may
        # fall back only when teardown demonstrably won before the call could
        # accept the request. Anchor verification failures use WebSocketError
        # and remain fail-closed even though they also fence the owner.
        status = (
            _DirectVolumeRequestStatus.FAILED
            if _direct_volume_owner_is_current(
                instance,
                owner,
                allow_new=allow_new,
            )
            else _DirectVolumeRequestStatus.OWNER_LOST
        )
        return status, None
    except Exception:  # noqa: BLE001 - anchor failures must remain fail-closed
        _LOGGER.warning("ThirdReality realtime volume control failed")
        return _DirectVolumeRequestStatus.FAILED, None
    if isinstance(applied, bool) or not isinstance(applied, int):
        _LOGGER.warning("ThirdReality realtime volume control returned invalid state")
        return _DirectVolumeRequestStatus.FAILED, None
    # A validated return is the linearization point. Stop pre-arms the owner
    # before acquiring this protocol lock, so re-checking it here could apply
    # the accepted software request and then also run the vendor physical path.
    return _DirectVolumeRequestStatus.APPLIED, max(0, min(applied, ceiling))


def _log_direct_volume_change(
    requested: int,
    applied: int,
    *,
    source: str = "command",
) -> None:
    """Publish one bounded, content-free command diagnostic."""
    with suppress(Exception):  # diagnostics cannot affect volume control
        syslog.syslog(
            syslog.LOG_INFO,
            f"codex-voice realtime_volume source={source} "
            f"requested_percent={requested} applied_percent={applied}",
        )


def _sound_config_signature() -> tuple[int, int, int] | None:
    """Return a cheap signature that changes on atomic physical-key writes."""
    path = getattr(thirdreality_satellite, "_SOUND_CONF", None)
    try:
        metadata = path.stat()
    except Exception:  # noqa: BLE001 - absence retains numeric vendor detection
        return None
    return metadata.st_mtime_ns, metadata.st_size, metadata.st_ino


def _remember_sound_config_signature(instance: Any) -> None:
    """Record the current config write boundary when it is observable."""
    signature = _sound_config_signature()
    if signature is not None:
        setattr(instance, _REALTIME_SOUND_SIGNATURE_ATTRIBUTE, signature)


def _atomic_update_direct_sound_config(changes: dict[str, object]) -> bool | None:
    """Share the hardware-key lock and atomically persist direct volume state.

    ``None`` means the pinned path object is unavailable (as in hermetic
    embedders), so the guarded vendor implementation may be used instead.
    Production failures are reported as ``False`` and must fence the direct
    owner; falling back to the vendor's unlocked truncate/write would recreate
    the exact race this transaction closes.
    """
    path = getattr(thirdreality_satellite, "_SOUND_CONF", None)
    try:
        sound_path = os.fspath(path)
    except TypeError:
        return None
    if not isinstance(sound_path, str) or not sound_path.startswith("/"):
        return False
    sound_file_path = Path(sound_path)

    lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    lock_flags |= getattr(os, "O_NOFOLLOW", 0)
    lock_fd: int | None = None
    temp_path: str | None = None
    locked = False
    try:
        lock_fd = os.open(_SOUND_CONFIG_LOCK_PATH, lock_flags, 0o644)
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            return False
        deadline = time.monotonic() + _SOUND_CONFIG_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(_SOUND_CONFIG_LOCK_RETRY_SECONDS, remaining))

        metadata = sound_file_path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            return False
        with open(sound_path, "rb") as sound_file:  # noqa: PTH123
            encoded = sound_file.read(_SOUND_CONFIG_MAX_BYTES + 1)
        if len(encoded) > _SOUND_CONFIG_MAX_BYTES:
            return False
        sound_config = json.loads(encoded.decode("utf-8"))
        if not isinstance(sound_config, dict):
            return False
        changed = False
        for key, value in changes.items():
            if sound_config.get(key) != value:
                sound_config[key] = value
                changed = True
        if not changed:
            return True

        directory = sound_file_path.parent
        temp_fd, temp_path = tempfile.mkstemp(
            prefix=".codex-sound-",
            dir=directory,
        )
        try:
            os.fchmod(temp_fd, stat.S_IMODE(metadata.st_mode))
            os.fchown(temp_fd, metadata.st_uid, metadata.st_gid)
            payload = (
                json.dumps(sound_config, ensure_ascii=False, indent=4) + "\n"
            ).encode("utf-8")
            with os.fdopen(temp_fd, "wb", closefd=True) as temp_file:
                temp_file.write(payload)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            temp_fd = -1
            Path(temp_path).replace(sound_file_path)
            temp_path = None
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(directory, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
        return True  # noqa: TRY300 - one transaction cleanup path
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return False
    finally:
        if temp_path is not None:
            with suppress(OSError):
                Path(temp_path).unlink()
        if lock_fd is not None:
            if locked:
                with suppress(OSError):
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def _persist_direct_volume(
    instance: Any,
    entity: Any,
    volume: float | None = None,
) -> bool:
    """Persist an already bounded value without invoking the vendor callback."""
    server_state = getattr(getattr(entity, "server", None), "state", None)
    persist_volume = getattr(server_state, "persist_volume", None)
    if not callable(persist_volume):
        _LOGGER.warning("ThirdReality realtime volume persistence is unavailable")
        return False
    try:
        logical_volume = entity.volume if volume is None else volume
        persist_volume(logical_volume)
    except Exception:  # noqa: BLE001 - state reporting must remain available
        _LOGGER.warning("ThirdReality realtime volume persistence failed")
        return False
    changes = {"volume": round(logical_volume * 100)}
    updated = _atomic_update_direct_sound_config(changes)
    if updated is None:
        update_sound_config = getattr(instance, "_update_sound_config", None)
        if not callable(update_sound_config):
            _LOGGER.warning("ThirdReality realtime sound config update is unavailable")
            return False
        try:
            updated = update_sound_config(changes)
        except Exception:  # noqa: BLE001 - state reporting must remain available
            _LOGGER.warning("ThirdReality realtime sound config update failed")
            return False
    if updated is not True:
        _LOGGER.warning("ThirdReality realtime sound config update was rejected")
        return False
    _remember_sound_config_signature(instance)
    # A physical key transaction can finish immediately before or after this
    # locked replace. Force exactly one next-tick sink verification so our own
    # JSON signature cannot hide the key script's preceding pactl mutation.
    setattr(instance, _REALTIME_ANCHOR_DIRTY_ATTRIBUTE, True)
    return True


def _initialize_direct_session_volume(instance: Any, owner: _RealtimeOwner) -> None:
    """Start direct software playback at the persisted bounded device volume."""
    if not _uses_deterministic_realtime_media():
        return
    entity = getattr(instance.state, "media_player_entity", None)
    ceiling = _direct_volume_ceiling_percent()
    desired = (
        None
        if entity is None or ceiling is None
        else _bounded_direct_volume_percent(getattr(entity, "volume", None), ceiling)
    )
    if entity is None or ceiling is None or desired is None:
        raise RuntimeError("direct playback volume state is unavailable")
    status, applied = _request_direct_volume(
        instance,
        owner,
        desired,
        ceiling,
        allow_new=True,
    )
    if status is not _DirectVolumeRequestStatus.APPLIED or applied is None:
        raise RuntimeError("direct playback volume could not be initialized")
    normalized = applied / 100
    changed = entity.volume != normalized
    entity.volume = normalized
    if not bool(getattr(entity, "muted", False)):
        entity.previous_volume = normalized
    if changed:
        if not _persist_direct_volume(instance, entity):
            raise RuntimeError("direct playback volume could not be persisted")
        instance._last_system_volume = normalized  # noqa: SLF001


def _message_controls_volume(message: Any) -> bool:
    """Return whether a vendor media message can alter device volume state."""
    if not isinstance(message, MediaPlayerCommandRequest):
        return False
    has_command = bool(getattr(message, "has_command", False))
    command = getattr(message, "command", None)
    return bool(getattr(message, "has_volume", False)) or (
        has_command and command in {MediaPlayerCommand.MUTE, MediaPlayerCommand.UNMUTE}
    )


def _handle_direct_media_volume(instance: Any, message: Any) -> tuple[bool, Any]:
    """Handle live direct volume commands without touching MPV or PulseAudio."""
    if not _message_controls_volume(message):
        return False, None

    owner = _live_direct_volume_owner(instance)
    if owner is None:
        return False, None
    entity = getattr(instance.state, "media_player_entity", None)
    has_command = bool(getattr(message, "has_command", False))
    has_volume = bool(getattr(message, "has_volume", False))
    command = getattr(message, "command", None)
    if entity is None or getattr(message, "key", None) != getattr(entity, "key", None):
        return False, None
    if bool(getattr(message, "has_media_url", False)) or (has_command and has_volume):
        _LOGGER.warning("Ignoring ambiguous ThirdReality realtime media command")
        return True, entity._update_state(entity.state)  # noqa: SLF001
    ceiling = _direct_volume_ceiling_percent()
    if ceiling is None:
        _LOGGER.warning("ThirdReality realtime volume configuration is invalid")
        return True, entity._update_state(entity.state)  # noqa: SLF001

    if has_command and command == MediaPlayerCommand.MUTE:
        status, applied = _request_direct_volume(instance, owner, 0, ceiling)
        if status is _DirectVolumeRequestStatus.OWNER_LOST:
            return False, None
        if status is not _DirectVolumeRequestStatus.APPLIED or applied is None:
            _interrupt_realtime_owner(instance, owner, source="volume_command")
        else:
            _log_direct_volume_change(0, applied)
            if not bool(getattr(entity, "muted", False)):
                entity.previous_volume = entity.volume
                entity.volume = applied / 100
                entity.muted = True
        return True, entity._update_state(entity.state)  # noqa: SLF001

    if has_command and command == MediaPlayerCommand.UNMUTE:
        if bool(getattr(entity, "muted", False)):
            desired = _bounded_direct_volume_percent(entity.previous_volume, ceiling)
            if desired is None:
                _LOGGER.warning("ThirdReality realtime saved volume is invalid")
            else:
                status, applied = _request_direct_volume(
                    instance,
                    owner,
                    desired,
                    ceiling,
                )
                if status is _DirectVolumeRequestStatus.OWNER_LOST:
                    return False, None
                if status is not _DirectVolumeRequestStatus.APPLIED or applied is None:
                    _interrupt_realtime_owner(
                        instance,
                        owner,
                        source="volume_command",
                    )
                else:
                    _log_direct_volume_change(desired, applied)
                    entity.volume = applied / 100
                    entity.muted = False
        return True, entity._update_state(entity.state)  # noqa: SLF001

    if has_command or not has_volume:
        return False, None
    requested = _bounded_direct_volume_percent(getattr(message, "volume", None), 100)
    if requested is None:
        _LOGGER.warning("ThirdReality realtime volume request is invalid")
        return True, entity._update_state(entity.state)  # noqa: SLF001
    desired = min(requested, ceiling)
    muted = bool(getattr(entity, "muted", False))
    status, applied = _request_direct_volume(
        instance,
        owner,
        0 if muted else desired,
        ceiling,
    )
    if status is _DirectVolumeRequestStatus.OWNER_LOST:
        return False, None
    if status is not _DirectVolumeRequestStatus.APPLIED or applied is None:
        _interrupt_realtime_owner(instance, owner, source="volume_command")
    elif muted:
        _log_direct_volume_change(requested, applied)
        entity.previous_volume = desired / 100
        if _persist_direct_volume(instance, entity, entity.previous_volume):
            instance._last_system_volume = entity.previous_volume  # noqa: SLF001
        else:
            _interrupt_realtime_owner(instance, owner, source="volume_command")
    else:
        _log_direct_volume_change(requested, applied)
        entity.volume = applied / 100
        entity.previous_volume = entity.volume
        if _persist_direct_volume(instance, entity):
            instance._last_system_volume = entity.volume  # noqa: SLF001
        else:
            _interrupt_realtime_owner(instance, owner, source="volume_command")
    return True, entity._update_state(entity.state)  # noqa: SLF001


def _realtime_handle_message(instance: Any, message: Any) -> Any:
    """Keep live direct volume changes inside the software renderer."""
    vendor_responses: tuple[Any, ...] | None = None
    with _realtime_state_lock(instance):
        handled, response = _handle_direct_media_volume(instance, message)
        if not handled and _message_controls_volume(message):
            # Execute the finite vendor volume transaction while the same lock
            # still protects the no-owner decision. A new direct owner cannot
            # appear between fallback selection and the physical player/state
            # update, so each command has exactly one observable owner.
            vendor_responses = tuple(_VENDOR_BASE_HANDLE_MESSAGE(instance, message))
    if handled:
        yield response
        return
    if vendor_responses is not None:
        yield from vendor_responses
        return
    yield from _VENDOR_BASE_HANDLE_MESSAGE(instance, message)


def _realtime_sync_volume_from_system(
    instance: Any,
    *,
    force: bool = False,
) -> None:
    """Reconcile a physical volume-key change with the direct renderer."""
    with _realtime_state_lock(instance):
        owner = _live_direct_volume_owner(instance)
        if owner is None:
            _VENDOR_TR_SYNC_VOLUME_FROM_SYSTEM(instance, force=force)
            return
        if not owner.capture_open:
            # Startup already applies the persisted logical volume before the
            # session starts. The vendor's 50 ms monitor must not enter a
            # CONNECTING session and interrupt its blocking media preflight.
            # A physical key change during this short window remains in
            # sound.json and is reconciled on the first LIVE poll.
            return

        entity = getattr(instance.state, "media_player_entity", None)
        ceiling = _direct_volume_ceiling_percent()
        if entity is None or ceiling is None:
            _LOGGER.warning("ThirdReality realtime system volume is unavailable")
            _interrupt_realtime_owner(instance, owner, source="volume_sync")
            return
        muted = bool(getattr(entity, "muted", False))
        sound_signature = _sound_config_signature()
        previous_sound_signature = getattr(
            instance,
            _REALTIME_SOUND_SIGNATURE_ATTRIBUTE,
            None,
        )
        sound_config_written = (
            sound_signature is not None
            and previous_sound_signature is not None
            and sound_signature != previous_sound_signature
        )
        anchor_dirty = bool(getattr(instance, _REALTIME_ANCHOR_DIRTY_ATTRIBUTE, False))
        try:
            normalized = instance._read_system_volume()  # noqa: SLF001
        except Exception:  # noqa: BLE001 - a transient config read still needs repair
            normalized = None
        requested = _bounded_direct_volume_percent(normalized, 100)
        if requested is None:
            # The key script changes PulseAudio and sound.json independently.
            # During a torn/transient JSON read, restore the exact sink anchor
            # immediately while retaining the last valid logical user choice.
            current = (
                getattr(entity, "previous_volume", None)
                if muted
                else getattr(entity, "volume", None)
            )
            desired = _bounded_direct_volume_percent(current, ceiling)
            if desired is None:
                _LOGGER.warning("ThirdReality current realtime volume is invalid")
                _interrupt_realtime_owner(instance, owner, source="volume_sync")
                return
            status, applied = _request_direct_volume(
                instance,
                owner,
                0 if muted else desired,
                ceiling,
                reconcile_anchor=True,
            )
            if status is _DirectVolumeRequestStatus.OWNER_LOST:
                _VENDOR_TR_SYNC_VOLUME_FROM_SYSTEM(instance, force=force)
            elif status is _DirectVolumeRequestStatus.APPLIED and applied is not None:
                _log_direct_volume_change(desired, applied, source="system_guard")
                _interrupt_realtime_owner(instance, owner, source="volume_sync")
            else:
                _interrupt_realtime_owner(instance, owner, source="volume_sync")
            return
        try:
            logical_volume_changed = (
                force
                or sound_config_written
                or instance._last_system_volume is None  # noqa: SLF001
                or abs(instance._last_system_volume - normalized) >= 0.0001  # noqa: SLF001
                or abs(instance.state.volume - normalized) >= 0.0001
            )
        except (TypeError, ValueError):
            _LOGGER.warning("ThirdReality system volume state is invalid")
            _interrupt_realtime_owner(instance, owner, source="volume_sync")
            return
        # This is the hot 50 ms path. Do not enter the session (and therefore
        # do not run pactl) unless sound.json or the persisted state changed.
        if not logical_volume_changed and not anchor_dirty:
            _remember_sound_config_signature(instance)
            return

        desired = min(requested, ceiling)
        status, applied = _request_direct_volume(
            instance,
            owner,
            0 if muted else desired,
            ceiling,
            reconcile_anchor=True,
        )
        if status is _DirectVolumeRequestStatus.OWNER_LOST:
            _VENDOR_TR_SYNC_VOLUME_FROM_SYSTEM(instance, force=force)
            return
        if status is not _DirectVolumeRequestStatus.APPLIED or applied is None:
            _interrupt_realtime_owner(instance, owner, source="volume_sync")
            return

        if anchor_dirty and not logical_volume_changed:
            # This tick exists only to close the post-persistence race. It
            # neither rewrites state nor arms another verification tick.
            setattr(instance, _REALTIME_ANCHOR_DIRTY_ATTRIBUTE, False)
            _remember_sound_config_signature(instance)
            _log_direct_volume_change(desired, applied, source="anchor_guard")
            return

        logical_volume = desired / 100
        if muted:
            entity.previous_volume = logical_volume
        else:
            entity.volume = applied / 100
            entity.previous_volume = entity.volume
            logical_volume = entity.volume
        if not _persist_direct_volume(instance, entity, logical_volume):
            _interrupt_realtime_owner(instance, owner, source="volume_sync")
            return
        instance._last_system_volume = logical_volume  # noqa: SLF001
        try:
            instance.send_messages([entity._get_state_message()])  # noqa: SLF001
        except Exception:  # noqa: BLE001 - reporting cannot kill the monitor task
            _LOGGER.warning("ThirdReality realtime volume state report failed")
        _log_direct_volume_change(requested, applied, source="system")


def _realtime_stop(instance: Any) -> None:
    """Interrupt one direct session, then preserve pinned vendor stop behavior."""
    setattr(instance, _REALTIME_STOP_REQUESTED_ATTRIBUTE, True)
    observed_owner = getattr(instance, _REALTIME_OWNER_ATTRIBUTE, None)
    if observed_owner is not None:
        observed_owner.stop_requested = True
    try:
        with _realtime_state_lock(instance):
            _discard_realtime_preroll(instance)
            owner = getattr(instance, _REALTIME_OWNER_ATTRIBUTE, None)
            if owner is not None:
                owner.stop_requested = True
                _interrupt_realtime_owner(instance, owner, source="vendor_stop")
            try:
                _VENDOR_BASE_STOP(instance)
            finally:
                # The pinned stop function may alter both values. Direct mode
                # must be observably idle even if cleanup raises part-way.
                instance._is_streaming_audio = False  # noqa: SLF001
                instance._pipeline_active = False  # noqa: SLF001
                restore_owner = owner if owner is not None else observed_owner
                if restore_owner is not None:
                    _restore_stop_word_membership(instance, restore_owner)
    finally:
        setattr(instance, _REALTIME_STOP_REQUESTED_ATTRIBUTE, False)


def _rollback_wakeup(
    instance: Any,
    phrase: str,
    *,
    notify_home_assistant: bool,
    unduck: bool,
) -> None:
    """Return a partially started wake transaction to a safe idle state."""
    instance._is_streaming_audio = False  # noqa: SLF001
    instance._pipeline_active = False  # noqa: SLF001
    if notify_home_assistant and instance.state.connected:
        try:
            instance.send_messages([VoiceAssistantRequest(start=False)])
        except Exception:
            _LOGGER.exception(
                "Failed to cancel Home Assistant after abandoned wake: %s",
                phrase,
            )
    if unduck:
        try:
            instance.unduck()
        except Exception:
            _LOGGER.exception("Failed to unduck after abandoned wake: %s", phrase)


def _wake_is_armed(instance: Any) -> bool:
    """Return whether a started wake remains eligible to forward audio."""
    return (
        instance._is_streaming_audio  # noqa: SLF001
        and instance._pipeline_active  # noqa: SLF001
        and instance.state.connected
        and not instance.state.muted
    )


def _fast_wakeup(instance: Any, wake_word: Any) -> None:
    """Start the Assist pipeline and microphone stream without a local cue."""
    if instance._timer_finished:  # noqa: SLF001 - preserve pinned vendor guards
        instance._timer_finished = False  # noqa: SLF001
        instance.unduck()
        instance.state.tts_player.stop()
        _LOGGER.debug("Stopping timer finished sound")
        return

    if instance.state.muted or not instance.state.connected:
        return

    if instance._pipeline_active:  # noqa: SLF001
        _LOGGER.debug("Ignoring wake word - pipeline already active")
        return

    phrase = wake_word.wake_word
    _LOGGER.debug("Detected wake word: %s", phrase)
    instance._pipeline_active = True  # noqa: SLF001
    # Clear a stale value before either external side effect.
    instance._is_streaming_audio = False  # noqa: SLF001

    request_attempted = False
    duck_attempted = False
    try:
        # Pre-arm before the request so every asynchronous VAD, run-end, mute,
        # or disconnect teardown can only clear the flag; there is no
        # later write that can resurrect it. Wake detection and handle_audio run
        # on this same pinned microphone thread, so audio cannot be forwarded
        # until wakeup returns after both external operations succeed.
        instance._is_streaming_audio = True  # noqa: SLF001
        request_attempted = True
        instance.send_messages(
            [VoiceAssistantRequest(start=True, wake_word_phrase=phrase)]
        )
        if not _wake_is_armed(instance):
            _rollback_wakeup(
                instance,
                phrase,
                notify_home_assistant=True,
                unduck=False,
            )
            return
        duck_attempted = True
        instance.duck()
        if not _wake_is_armed(instance):
            _rollback_wakeup(
                instance,
                phrase,
                notify_home_assistant=True,
                unduck=True,
            )
    except Exception:
        _rollback_wakeup(
            instance,
            phrase,
            notify_home_assistant=request_attempted,
            unduck=duck_attempted,
        )
        raise


def _fast_thirdreality_wakeup(instance: Any, wake_word: Any) -> None:
    """Apply the fast wake path directly to the pinned device subclass."""
    with _realtime_state_lock(instance):
        # Once direct mode owns the microphone, every later utterance belongs
        # to that conversation. Keeping the normal wake detector armed is a
        # vendor implementation detail: treating one of its false positives as
        # an intentional route switch would tear down realtime before VAD can
        # provide barge-in or a follow-up turn.
        if _reconcile_realtime_owner(instance) is not None:
            _LOGGER.debug("Ignoring wake word - realtime session active")
            return

        realtime_wake, selection = _classify_wake(wake_word)
        realtime_only_blocked = _realtime_only_mode() and not realtime_wake
        _log_wake_selection(
            (
                "realtime"
                if realtime_wake
                else "disabled"
                if realtime_only_blocked
                else "assist"
            ),
            selection,
        )
        preroll_audio = _take_realtime_preroll(instance)
        if realtime_only_blocked:
            _LOGGER.debug("Ignoring non-realtime wake word in realtime-only mode")
            return
        previous_active = instance._pipeline_active  # noqa: SLF001
        if realtime_wake:
            _start_realtime_wakeup(instance, wake_word, preroll_audio)
        else:
            _fast_wakeup(instance, wake_word)
        if not previous_active and instance._pipeline_active:  # noqa: SLF001
            if not realtime_wake or not _uses_deterministic_realtime_media():
                _nonblocking_led_fire("listening")


def _decode_stderr(stderr: Any) -> str:
    """Return subprocess stderr as safe log text."""
    if isinstance(stderr, bytes):
        return stderr.decode(errors="replace").strip()
    if stderr is None:
        return ""
    return str(stderr).strip()


def _run_led_command(state: str, animation: str, command: list[str]) -> None:
    """Run and reap one serialized LED command on the worker thread."""
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            close_fds=True,
            timeout=_LED_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run kills and waits for its child before raising, so a
        # timed-out dbus-send cannot remain as a zombie.
        _LED_LOGGER.warning("[led] dbus-send timeout for state: %s", state)
        return
    except Exception:
        _LED_LOGGER.warning("[led] exception for state: %s", state, exc_info=True)
        return

    if result.returncode != 0:
        _LED_LOGGER.warning(
            "[led] dbus-send failed (rc=%d): %s",
            result.returncode,
            _decode_stderr(result.stderr),
        )
        return
    _LED_LOGGER.debug("[led] OK: %s → %s", state, animation)


def _led_worker() -> None:
    """Run queued LED commands serially without holding the queue lock."""
    while True:
        with _LED_CONDITION:
            _LED_CONDITION.wait_for(lambda: _LED_QUEUE or _LED_SHUT_DOWN)
            if _LED_SHUT_DOWN:
                _LED_QUEUE.clear()
                return
            state, animation, command = _LED_QUEUE.popleft()
        _run_led_command(state, animation, command)


def _create_led_worker() -> threading.Thread:
    """Create the single daemon LED worker without starting it."""
    return threading.Thread(
        target=_led_worker,
        name=_LED_THREAD_PREFIX,
        daemon=True,
    )


def _nonblocking_led_fire(state: str, to_idle: bool = False) -> None:
    """Queue one LED command and return without waiting for dbus-send."""
    filename = thirdreality_satellite._LED_ANIMATIONS.get(state)  # noqa: SLF001
    if not filename:
        _LED_LOGGER.warning("[led] unknown state: %s", state)
        return

    animation = thirdreality_satellite._ANIM_DIR + filename  # noqa: SLF001
    command = [
        "dbus-send",
        "--system",
        "--type=signal",
        "/com/3r/EventBus",
        "com._3reality.EventBus.LedShow",
        f"boolean:{'true' if to_idle else 'false'}",
        f"array:string:{animation}",
    ]
    _LED_LOGGER.debug("[led] firing: %s", " ".join(command))

    global _LED_WORKER  # noqa: PLW0603
    with _LED_CONDITION:
        if _LED_SHUT_DOWN:
            _LED_LOGGER.warning("[led] ignoring state after worker shutdown: %s", state)
            return
        if _LED_WORKER is None:
            worker = _create_led_worker()
            try:
                worker.start()
            except Exception:
                _LED_LOGGER.warning(
                    "[led] failed to start worker for state: %s",
                    state,
                    exc_info=True,
                )
                return
            _LED_WORKER = worker
        if len(_LED_QUEUE) >= _LED_MAX_PENDING:
            dropped = len(_LED_QUEUE)
            _LED_QUEUE.clear()
            _LED_LOGGER.warning(
                "[led] coalescing %d stale states into newest state: %s",
                dropped,
                state,
            )
        _LED_QUEUE.append((state, animation, command))
        _LED_CONDITION.notify()


def _shutdown_led_worker() -> None:
    """Stop the private worker during explicit or normal-exit cleanup."""
    global _LED_SHUT_DOWN, _LED_WORKER  # noqa: PLW0603
    with _LED_CONDITION:
        if _LED_SHUT_DOWN:
            return
        _LED_SHUT_DOWN = True
        _LED_QUEUE.clear()
        worker = _LED_WORKER
        _LED_CONDITION.notify_all()
    if worker is not None and worker is not threading.current_thread():
        worker.join(timeout=_LED_TIMEOUT_SECONDS + 0.5)
        if worker.is_alive():  # pragma: no cover - subprocess timeout safety net
            _LED_LOGGER.warning("[led] worker did not stop before shutdown deadline")
        else:
            _LED_WORKER = None


_observed_hashes = (
    _code_hash(VoiceSatelliteProtocol.wakeup),
    _code_hash(VoiceSatelliteProtocol._on_wakeup_sound_finished),  # noqa: SLF001
    _code_hash(thirdreality_satellite.TRSatelliteProtocol.wakeup),
    _code_hash(thirdreality_satellite._led_fire),  # noqa: SLF001
)
_expected_hashes = (
    _EXPECTED_BASE_WAKEUP,
    _EXPECTED_BASE_FINISH,
    _EXPECTED_TR_WAKEUP,
    _EXPECTED_TR_LED_FIRE,
)

_load_realtime_config()

if _observed_hashes == _expected_hashes:
    VoiceSatelliteProtocol.wakeup = _fast_wakeup
    thirdreality_satellite.TRSatelliteProtocol.wakeup = _fast_thirdreality_wakeup
    thirdreality_satellite._led_fire = _nonblocking_led_fire  # noqa: SLF001
    atexit.register(_shutdown_led_worker)
    if _REALTIME_CONFIG is not None:
        _observed_realtime_hashes = (
            _code_hash(_VENDOR_BASE_HANDLE_AUDIO),
            _code_hash(_VENDOR_BASE_STOP),
            _code_hash(_VENDOR_BASE_HANDLE_MESSAGE),
            _code_hash(_VENDOR_TR_INIT),
            _code_hash(_VENDOR_BASE_INIT),
            _code_hash(_VENDOR_TR_HANDLE_MESSAGE),
            _code_hash(_VENDOR_TR_SYNC_VOLUME),
            _code_hash(_VENDOR_TR_SYNC_VOLUME_FROM_SYSTEM),
            _code_hash(_VENDOR_TR_SYNC_STATE_FROM_SYSTEM),
            _code_hash(_VENDOR_TR_SYSTEM_SYNC_LOOP),
            _code_hash(_VENDOR_TR_UPDATE_SOUND_CONFIG),
            _code_hash(_VENDOR_TR_INSTALL_VOLUME_BRIDGE),
            _code_hash(_VENDOR_MEDIA_PLAYER_INIT),
            _code_hash(_VENDOR_MEDIA_PLAYER_HANDLE_MESSAGE),
            _code_hash(_VENDOR_MEDIA_PLAYER_APPLY_VOLUME),
            _code_hash(_VENDOR_MEDIA_PLAYER_APPLY_VOLUME_FROM_STATE),
            _code_hash(_VENDOR_MEDIA_PLAYER_SET_VOLUME_CALLBACK),
            _code_hash(_VENDOR_MEDIA_PLAYER_GET_STATE),
            _code_hash(_VENDOR_MEDIA_PLAYER_UPDATE_STATE),
            _code_hash(_VENDOR_SERVER_STATE_PERSIST_VOLUME),
            _module_file_hash("linux_voice_assistant.__main__"),
        )
        _expected_realtime_hashes = (
            _EXPECTED_BASE_HANDLE_AUDIO,
            _EXPECTED_BASE_STOP,
            _EXPECTED_BASE_HANDLE_MESSAGE,
            _EXPECTED_TR_INIT,
            _EXPECTED_BASE_INIT,
            _EXPECTED_TR_HANDLE_MESSAGE,
            _EXPECTED_TR_SYNC_VOLUME,
            _EXPECTED_TR_SYNC_VOLUME_FROM_SYSTEM,
            _EXPECTED_TR_SYNC_STATE_FROM_SYSTEM,
            _EXPECTED_TR_SYSTEM_SYNC_LOOP,
            _EXPECTED_TR_UPDATE_SOUND_CONFIG,
            _EXPECTED_TR_INSTALL_VOLUME_BRIDGE,
            _EXPECTED_MEDIA_PLAYER_INIT,
            _EXPECTED_MEDIA_PLAYER_HANDLE_MESSAGE,
            _EXPECTED_MEDIA_PLAYER_APPLY_VOLUME,
            _EXPECTED_MEDIA_PLAYER_APPLY_VOLUME_FROM_STATE,
            _EXPECTED_MEDIA_PLAYER_SET_VOLUME_CALLBACK,
            _EXPECTED_MEDIA_PLAYER_GET_STATE,
            _EXPECTED_MEDIA_PLAYER_UPDATE_STATE,
            _EXPECTED_SERVER_STATE_PERSIST_VOLUME,
            _EXPECTED_MAIN_MODULE_FILE,
        )
        if (
            _observed_realtime_hashes == _expected_realtime_hashes
            and getattr(thirdreality_satellite, "_VOLUME_POLL_INTERVAL", None)
            == _EXPECTED_SYSTEM_VOLUME_POLL_INTERVAL
        ):
            _REALTIME_PATCH_ACTIVE = True
            VoiceSatelliteProtocol.handle_audio = _realtime_handle_audio
            VoiceSatelliteProtocol.handle_message = _realtime_handle_message
            VoiceSatelliteProtocol.stop = _realtime_stop
            thirdreality_satellite.TRSatelliteProtocol.__init__ = (
                _fast_thirdreality_init
            )
            thirdreality_satellite.TRSatelliteProtocol._sync_volume_from_system = (  # noqa: SLF001
                _realtime_sync_volume_from_system
            )
            thirdreality_satellite._VOLUME_POLL_INTERVAL = (  # noqa: SLF001
                _DIRECT_SYSTEM_VOLUME_POLL_INTERVAL
            )
            try:
                atexit.register(_REALTIME_SUPPORT.shutdown_all_sessions)
            except Exception as exc:  # noqa: BLE001 - optional support boundary
                if _NATIVE_AEC3_SELECTED:
                    _fatal_aec3_startup(
                        "ThirdReality native AEC3 realtime support failed",
                        exc,
                    )
                _LOGGER.warning("ThirdReality realtime cleanup is unavailable")
            try:
                prewarm_ok = (
                    _REALTIME_SUPPORT.prewarm_device_webrtc()
                    if _uses_device_webrtc()
                    else True
                )
            except Exception as exc:  # noqa: BLE001 - optional support boundary
                if _NATIVE_AEC3_SELECTED:
                    _fatal_aec3_startup(
                        "ThirdReality native AEC3 realtime support failed",
                        exc,
                    )
                prewarm_ok = False
            if not prewarm_ok:
                _LOGGER.warning("ThirdReality direct WebRTC prewarm is unavailable")
                if _NATIVE_AEC3_SELECTED:
                    _fatal_aec3_startup(
                        "ThirdReality native AEC3 requires direct WebRTC prewarm"
                    )
        else:
            _LOGGER.warning(
                "Skipping ThirdReality realtime client: unrecognized vendor bytecode"
            )
            if _NATIVE_AEC3_SELECTED:
                _fatal_aec3_startup(
                    "ThirdReality native AEC3 requires the guarded realtime overlay"
                )
else:
    _LOGGER.warning(
        "Skipping ThirdReality latency overlay: unrecognized vendor bytecode"
    )
    if _NATIVE_AEC3_SELECTED:
        _fatal_aec3_startup(
            "ThirdReality native AEC3 requires the guarded latency overlay"
        )
