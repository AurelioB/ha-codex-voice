# ruff: noqa: INP001
"""Apply process-local latency tuning for ThirdReality firmware v1.1.7."""

import asyncio
import hashlib
import logging
import marshal
import threading
from typing import Any

from aioesphomeapi.api_pb2 import VoiceAssistantRequest
from linux_voice_assistant.satellite import VoiceSatelliteProtocol

_LOGGER = logging.getLogger("linux_voice_assistant.satellite")

# Guard the monkeypatch with exact pinned vendor bytecode. The package metadata
# on the measured image reports an unrelated version, so it is not authoritative.
_EXPECTED_WAKEUP = "9fc5d4920ced216444adf048f0733929a3261ae47a76ed5fa2bed8061cc46697"
_EXPECTED_FINISH = "a1544719b6fac5cff4388a5c10f0674cd295fb98c3c86e799993db1cbee2080d"
_WAKE_CUE_WATCHDOG_SECONDS = 2.0


def _code_hash(function: Any) -> str:
    """Return a stable hash for one installed Python code object."""
    return hashlib.sha256(marshal.dumps(function.__code__)).hexdigest()


def _cancel_wake_watchdog(instance: Any) -> None:
    """Cancel and forget the current wake-cue watchdog, if any."""
    watchdog = getattr(instance, "_codex_wake_watchdog", None)
    instance._codex_wake_watchdog = None  # noqa: SLF001
    if watchdog is not None:
        watchdog.cancel()


def _abort_wakeup(
    instance: Any,
    phrase: str,
    generation: object,
    *,
    notify_home_assistant: bool,
    unduck: bool,
) -> bool:
    """Roll back one matching partially started wake transaction."""
    if getattr(instance, "_codex_wake_generation", None) is not generation:
        return False
    _cancel_wake_watchdog(instance)
    instance._codex_wake_generation = None  # noqa: SLF001
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
    return True


def _wake_watchdog(instance: Any, phrase: str, generation: object) -> None:
    """Abort a wake when the local player never reports cue completion."""
    if _abort_wakeup(
        instance,
        phrase,
        generation,
        notify_home_assistant=True,
        unduck=True,
    ):
        _LOGGER.warning("Wakeup sound completion timed out for: %s", phrase)


def _schedule_wake_watchdog(
    instance: Any,
    phrase: str,
    generation: object,
) -> None:
    """Install the watchdog on the protocol's owning asyncio loop."""
    loop = getattr(instance, "_loop", None)
    if loop is None:
        loop = asyncio.get_running_loop()
    if loop.is_closed():
        raise RuntimeError("voice protocol event loop is closed")

    def install() -> None:
        if getattr(instance, "_codex_wake_generation", None) is not generation:
            return
        instance._codex_wake_watchdog = loop.call_later(  # noqa: SLF001
            _WAKE_CUE_WATCHDOG_SECONDS,
            _wake_watchdog,
            instance,
            phrase,
            generation,
        )

    loop_thread_id = getattr(instance, "_loop_thread_id", None)
    if loop_thread_id is not None and threading.get_ident() != loop_thread_id:
        loop.call_soon_threadsafe(install)
    else:
        install()


def _finish_wakeup(instance: Any, phrase: str, generation: object) -> None:
    """Begin forwarding audio only for the still-active matching wake."""
    if getattr(instance, "_codex_wake_generation", None) is not generation:
        _LOGGER.debug("Ignoring stale wakeup-sound callback for: %s", phrase)
        return
    if (
        not instance._pipeline_active  # noqa: SLF001
        or not instance.state.connected
        or instance.state.muted
    ):
        # Mute/disconnect can stop the cue before the vendor run-end callback.
        # Roll back both flags here so a locally abandoned wake cannot make all
        # later wake words look like a pipeline is still active.
        _abort_wakeup(
            instance,
            phrase,
            generation,
            notify_home_assistant=True,
            unduck=True,
        )
        _LOGGER.debug("Wakeup sound finished after pipeline cancellation: %s", phrase)
        return
    _cancel_wake_watchdog(instance)
    instance._codex_wake_generation = None  # noqa: SLF001
    _LOGGER.debug(
        "Wakeup sound finished, enabling audio streaming for: %s",
        phrase,
    )
    instance._is_streaming_audio = True  # noqa: SLF001 - vendor state backport


def _overlap_wakeup(instance: Any, wake_word: Any) -> None:
    """Let Home Assistant prepare the pipeline while the wake cue plays."""
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
    instance._is_streaming_audio = False  # noqa: SLF001
    generation = object()
    instance._codex_wake_generation = generation  # noqa: SLF001

    # No microphone audio is forwarded here. Home Assistant can open the Assist
    # pipeline during the cue, then the EOF callback starts audio immediately.
    ducked = False
    request_sent = False
    try:
        # Treat an attempted side effect as potentially partial. Both vendor
        # calls cross process/device boundaries and may raise after changing
        # state, so rollback must still send the inverse operation.
        request_sent = True
        instance.send_messages(
            [VoiceAssistantRequest(start=True, wake_word_phrase=phrase)]
        )
        ducked = True
        instance.duck()
        _schedule_wake_watchdog(instance, phrase, generation)
        instance.state.tts_player.play(
            instance.state.wakeup_sound,
            done_callback=lambda: _finish_wakeup(instance, phrase, generation),
        )
    except Exception:
        _abort_wakeup(
            instance,
            phrase,
            generation,
            notify_home_assistant=request_sent,
            unduck=ducked,
        )
        raise


if (
    _code_hash(VoiceSatelliteProtocol.wakeup) == _EXPECTED_WAKEUP
    and _code_hash(VoiceSatelliteProtocol._on_wakeup_sound_finished)  # noqa: SLF001
    == _EXPECTED_FINISH
):
    VoiceSatelliteProtocol.wakeup = _overlap_wakeup
else:
    _LOGGER.warning("Skipping wake overlap: unrecognized vendor bytecode")
