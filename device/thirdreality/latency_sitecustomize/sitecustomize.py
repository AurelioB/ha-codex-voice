# ruff: noqa: INP001
"""Apply process-local latency tuning for ThirdReality firmware v1.1.7."""

from __future__ import annotations

import atexit
import hashlib
import logging
import marshal
import subprocess
import threading
from collections import deque
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

if _observed_hashes == _expected_hashes:
    VoiceSatelliteProtocol.wakeup = _fast_wakeup
    thirdreality_satellite._led_fire = _nonblocking_led_fire  # noqa: SLF001
    atexit.register(_shutdown_led_worker)
else:
    _LOGGER.warning(
        "Skipping ThirdReality latency overlay: unrecognized vendor bytecode"
    )
