# ruff: noqa: INP001
"""Apply process-local latency tuning for ThirdReality firmware v1.1.7."""

from __future__ import annotations

import atexit
import hashlib
import importlib
import logging
import marshal
import subprocess
import syslog
import threading
from collections import deque
from contextlib import suppress
from typing import Any

from aioesphomeapi.api_pb2 import VoiceAssistantRequest
from linux_voice_assistant.satellite import VoiceSatelliteProtocol
from thirdreality import satellite as thirdreality_satellite

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
_EXPECTED_BASE_HANDLE_AUDIO_OPCODES = (
    "ecc9e6112426a14c798736e18244af1cea526ec072882e4d95c622106a06a41d"
)
_EXPECTED_BASE_STOP_OPCODES = (
    "b249e6254095ee6c19fa26795eeb424b762972adf52ba931b2a04ae3985c80ea"
)
_EXPECTED_BASE_HANDLE_MESSAGE = (
    "d930b8d7852ac6567b219119b3ac29599df0f87f39f6ad92beb9cd27cb678724"
)
_EXPECTED_TR_INIT = "9120bc4f5b727f360bdd632bd0fef25747a299ad64aabc5ec0bd57ac299eb24b"
_EXPECTED_BASE_INIT = "1c8edd949cc12268f15e2ead3af5d9c8125b9c22a9c74f5e7dc5a6695a3eff25"
_EXPECTED_MAIN_MODULE_FILE = (
    "38fe14a2068eaa0bbd4af989ddc1a8581d193edcd98f1fe9a837300bec48648d"
)
_MAX_VENDOR_MODULE_BYTES = 4 * 1024 * 1024

_VENDOR_BASE_INIT = VoiceSatelliteProtocol.__init__
_VENDOR_BASE_HANDLE_AUDIO = VoiceSatelliteProtocol.handle_audio
_VENDOR_BASE_STOP = VoiceSatelliteProtocol.stop
_VENDOR_BASE_HANDLE_MESSAGE = VoiceSatelliteProtocol.handle_message
_VENDOR_TR_INIT = thirdreality_satellite.TRSatelliteProtocol.__init__
_REALTIME_SUPPORT: Any = None
_REALTIME_CONFIG: Any = None
_REALTIME_PATCH_ACTIVE = False
_REALTIME_OWNER_ATTRIBUTE = "_codex_realtime_owner"
_REALTIME_PREROLL_ATTRIBUTE = "_codex_realtime_preroll"
_REALTIME_LOCK_ATTRIBUTE = "_codex_realtime_lock"
_REALTIME_STOP_REQUESTED_ATTRIBUTE = "_codex_realtime_stop_requested"
# The pinned recorder emits 2,048-byte PCM16 frames every 64 ms. Wake
# activation happens after handle_audio sees the triggering frame, so retain
# six idle frames for the direct wake path. This is RAM-only and small enough
# for the client's bounded 2x startup catch-up.
_REALTIME_PREROLL_MAX_BYTES = 12 * 1024
# Preserve one second of live PCM capacity behind pre-roll so legal, smaller
# custom queues do not fall back merely because the cold handshake is pending.
_REALTIME_STARTUP_HEADROOM_BYTES = 32 * 1024
_REALTIME_LOCK_CREATION = threading.Lock()

_LED_TIMEOUT_SECONDS = 2.0
_LED_THREAD_PREFIX = "thirdreality-led"
_LED_MAX_PENDING = 8
_LED_CONDITION = threading.Condition()
_LED_QUEUE: deque[tuple[str, str, list[str]]] = deque()
_LED_WORKER: threading.Thread | None = None
_LED_SHUT_DOWN = False


def _code_hash(function: Any) -> str:
    """Return a stable hash for one installed Python code object."""
    return hashlib.sha256(marshal.dumps(function.__code__)).hexdigest()


def _opcode_hash(function: Any) -> str:
    """Return the exact installed opcode-stream hash for a narrow patch."""
    return hashlib.sha256(function.__code__.co_code).hexdigest()


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
        "ducked",
        "fallback_audio",
        "fallback_bytes",
        "ready_seen",
        "released",
        "session",
        "stop_requested",
        "stop_word_id",
        "stop_word_was_active",
        "wake_word",
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
        self.ready_seen = False
        self.released = False
        self.stop_requested = False
        self.fallback_bytes = 0
        self.fallback_audio: deque[bytes] = deque()


def _load_realtime_config() -> None:
    """Load optional support without making the normal HA path depend on it."""
    global _REALTIME_CONFIG, _REALTIME_SUPPORT  # noqa: PLW0603
    try:
        support = importlib.import_module("realtime_client")
    except ModuleNotFoundError as exc:
        if exc.name != "realtime_client":
            _LOGGER.warning("ThirdReality realtime client import failed")
        return
    except Exception:  # noqa: BLE001 - optional code must not break vendor startup
        _LOGGER.warning("ThirdReality realtime client import failed")
        return
    try:
        config = support.load_config()
    except FileNotFoundError:
        return
    except (OSError, support.ConfigError):
        _LOGGER.warning("ThirdReality realtime configuration is invalid")
        return
    except Exception:  # noqa: BLE001 - fail closed around optional package code
        _LOGGER.warning("ThirdReality realtime configuration could not be loaded")
        return
    if config is None:
        return
    _REALTIME_SUPPORT = support
    _REALTIME_CONFIG = config


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
        return
    _install_realtime_wake_order(state)
    _VENDOR_TR_INIT(instance, state)


def _uses_device_webrtc() -> bool:
    """Return whether the configured wake owns provider media on this device."""
    if _REALTIME_CONFIG is None or _REALTIME_SUPPORT is None:
        return False
    return getattr(_REALTIME_CONFIG, "media_transport", None) == getattr(
        _REALTIME_SUPPORT, "DEVICE_WEBRTC_TRANSPORT", "device_webrtc"
    )


def _realtime_only_mode() -> bool:
    """Return whether this appliance must never enter the Assist wake path."""
    return bool(getattr(_REALTIME_CONFIG, "realtime_only", False))


def _assist_fallback_allowed() -> bool:
    """Return whether buffered direct audio may fall back to Home Assistant."""
    return not _uses_device_webrtc() and not _realtime_only_mode()


def _stop_word_membership(instance: Any) -> tuple[Any, bool]:
    stop_word = getattr(instance.state, "stop_word", None)
    stop_word_id = getattr(stop_word, "id", None)
    active = getattr(instance.state, "active_wake_words", None)
    if stop_word_id is None or active is None:
        return None, False
    was_active = stop_word_id in active
    if not was_active:
        active.add(stop_word_id)
    return stop_word_id, was_active


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

        preroll_audio = _preroll_with_startup_headroom(preroll_audio)
        try:
            session = _REALTIME_SUPPORT.RealtimeSession(_REALTIME_CONFIG)
        except Exception:  # noqa: BLE001 - optional client must fail closed
            if getattr(instance, _REALTIME_STOP_REQUESTED_ATTRIBUTE, False):
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
            return
        try:
            owner = _RealtimeOwner(
                session=session,
                wake_word=wake_word,
                stop_word_id=None,
                stop_word_was_active=False,
            )
            stop_word_id, stop_word_was_active = _stop_word_membership(instance)
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
        try:
            session.start()
            if owner.stop_requested:
                _interrupt_realtime_owner(instance, owner)
                return
            # Mark the side effect as attempted before calling vendor code so a
            # partial duck that raises is still undone during rollback.
            owner.ducked = True
            instance.duck()
            if owner.stop_requested:
                _interrupt_realtime_owner(instance, owner)
                return
            for chunk in preroll_audio:
                result = session.submit_audio(chunk)
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
        owner.released = True
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
        _nonblocking_led_fire("idle", to_idle=True)


def _reconcile_realtime_owner(instance: Any) -> _RealtimeOwner | None:
    """Release a terminal owner and return only a still-live owner."""
    with _realtime_state_lock(instance):
        owner = getattr(instance, _REALTIME_OWNER_ATTRIBUTE, None)
        if owner is None or not owner.session.terminal:
            return owner
        _detach_realtime_owner(instance, owner, unduck=owner.ducked)
        return getattr(instance, _REALTIME_OWNER_ATTRIBUTE, None)


def _interrupt_realtime_owner(instance: Any, owner: _RealtimeOwner) -> None:
    """Interrupt and release one current owner without starting HA fallback."""
    with _realtime_state_lock(instance):
        if owner.released:
            return
        if getattr(instance, _REALTIME_OWNER_ATTRIBUTE, None) is not owner:
            return
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
            _interrupt_realtime_owner(instance, owner)
            return
        if owner.session.failed_before_ready:
            _fallback_realtime_to_ha(instance, owner, audio_chunk)
            return
        if owner.session.terminal:
            _detach_realtime_owner(instance, owner, unduck=owner.ducked)
            return

        if not owner.session.ready:
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
                _interrupt_realtime_owner(instance, owner)
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
        _interrupt_realtime_owner(instance, owner)
        _LOGGER.warning("ThirdReality realtime audio session ended safely")


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
                _interrupt_realtime_owner(instance, owner)
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
            _opcode_hash(_VENDOR_BASE_HANDLE_AUDIO),
            _opcode_hash(_VENDOR_BASE_STOP),
            _code_hash(_VENDOR_BASE_HANDLE_MESSAGE),
            _code_hash(_VENDOR_TR_INIT),
            _code_hash(_VENDOR_BASE_INIT),
            _module_file_hash("linux_voice_assistant.__main__"),
        )
        _expected_realtime_hashes = (
            _EXPECTED_BASE_HANDLE_AUDIO_OPCODES,
            _EXPECTED_BASE_STOP_OPCODES,
            _EXPECTED_BASE_HANDLE_MESSAGE,
            _EXPECTED_TR_INIT,
            _EXPECTED_BASE_INIT,
            _EXPECTED_MAIN_MODULE_FILE,
        )
        if _observed_realtime_hashes == _expected_realtime_hashes:
            _REALTIME_PATCH_ACTIVE = True
            VoiceSatelliteProtocol.handle_audio = _realtime_handle_audio
            VoiceSatelliteProtocol.stop = _realtime_stop
            thirdreality_satellite.TRSatelliteProtocol.__init__ = (
                _fast_thirdreality_init
            )
            atexit.register(_REALTIME_SUPPORT.shutdown_all_sessions)
            if _uses_device_webrtc() and not _REALTIME_SUPPORT.prewarm_device_webrtc():
                _LOGGER.warning("ThirdReality direct WebRTC prewarm is unavailable")
        else:
            _LOGGER.warning(
                "Skipping ThirdReality realtime client: unrecognized vendor bytecode"
            )
else:
    _LOGGER.warning(
        "Skipping ThirdReality latency overlay: unrecognized vendor bytecode"
    )
