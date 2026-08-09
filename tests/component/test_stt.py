"""Tests for the Codex Voice speech-to-text entity."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components import stt
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_PROMPT
from homeassistant.helpers import chat_session

from custom_components.codex_voice import CodexVoiceConfigEntry
from custom_components.codex_voice import api as api_module
from custom_components.codex_voice import stt as stt_module
from custom_components.codex_voice import tts as tts_module
from custom_components.codex_voice.api import (
    BridgeAuthenticationError,
    BridgeBusyError,
    BridgeClient,
    BridgeConnectionError,
    BridgeProtocolError,
    BridgeQuotaError,
    BridgeStreamingUnsupported,
)
from custom_components.codex_voice.const import CONF_INSTRUCTIONS, CONF_VOICE
from custom_components.codex_voice.stt import CodexVoiceSTTEntity
from custom_components.codex_voice.tts import CodexVoiceTTSEntity


async def _audio_stream(*chunks: bytes) -> AsyncIterator[bytes]:
    """Yield test audio chunks."""
    for chunk in chunks:
        yield chunk


def _make_entry(
    client: Any,
    *,
    subentries: dict[str, ConfigSubentry] | None = None,
) -> CodexVoiceConfigEntry:
    """Create the minimal config-entry interface required by an entity."""
    return cast(
        "CodexVoiceConfigEntry",
        SimpleNamespace(
            runtime_data=client,
            async_start_reauth=Mock(),
            subentries=subentries or {},
        ),
    )


def _make_entity(
    client: Any,
    *,
    data: dict[str, Any] | None = None,
    entry_subentries: dict[str, ConfigSubentry] | None = None,
) -> CodexVoiceSTTEntity:
    """Create an STT entity with a minimal config subentry."""
    entry = _make_entry(client, subentries=entry_subentries)
    subentry = ConfigSubentry(
        data=MappingProxyType(data or {}),
        subentry_type="stt",
        title="Test STT",
        unique_id=None,
    )
    return CodexVoiceSTTEntity(entry, subentry)


def _metadata(language: str = "en-US") -> stt.SpeechMetadata:
    """Return supported mono PCM metadata."""
    return stt.SpeechMetadata(
        language=language,
        format=stt.AudioFormats.WAV,
        codec=stt.AudioCodecs.PCM,
        bit_rate=stt.AudioBitRates.BITRATE_16,
        sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
        channel=stt.AudioChannels.CHANNEL_MONO,
    )


def _bridge_metadata() -> dict[str, Any]:
    """Return the metadata shape passed to the bridge client."""
    return {
        "language": "en-US",
        "codec": "pcm",
        "sample_rate": 16000,
        "bit_rate": 16,
        "channels": 1,
    }


async def test_stt_forwards_stream_and_metadata() -> None:
    """The STT entity passes the microphone iterable directly to the bridge."""
    stream_transcribe = AsyncMock(return_value="Turn on the kitchen")
    finite_transcribe = AsyncMock()
    client = SimpleNamespace(
        async_transcribe_stream=stream_transcribe,
        async_transcribe=finite_transcribe,
    )
    entity = _make_entity(client)
    stream = _audio_stream(b"\x00\x01", b"\x02\x03")

    result = await entity.async_process_audio_stream(_metadata(), stream)

    assert result.result is stt.SpeechResultState.SUCCESS
    assert result.text == "Turn on the kitchen"
    stream_transcribe.assert_awaited_once_with(
        stream,
        _bridge_metadata(),
        prompt=None,
    )
    finite_transcribe.assert_not_awaited()


async def test_stt_opts_in_only_after_pipeline_tts_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA's pre-STT TTS options bind one normalized private handoff request."""
    monkeypatch.setattr(
        tts_module,
        "_AUTOMATIC_SPEECH_SESSION_HANDOFF_ENABLED",
        True,
    )
    stream_transcribe = AsyncMock(return_value="Active transcript")
    client = SimpleNamespace(
        async_transcribe_stream=stream_transcribe,
        async_transcribe=AsyncMock(),
    )
    tts_subentry = ConfigSubentry(
        data=MappingProxyType({CONF_VOICE: "ember"}),
        subentry_type="tts",
        title="Test TTS",
        unique_id=None,
    )
    entity = _make_entity(
        client,
        entry_subentries={tts_subentry.subentry_id: tts_subentry},
    )
    tts_entity = CodexVoiceTTSEntity(_make_entry(client), tts_subentry)
    session = chat_session.ChatSession("assist-session")
    session_context = chat_session.current_session.set(session)
    try:
        pipeline_options = dict(tts_entity.default_options)
        assert (
            pipeline_options[api_module._SPEECH_SESSION_HANDOFF_OPTION]
            == api_module._SPEECH_SESSION_HANDOFF_OPTION_VALUE
        )
        result = await entity.async_process_audio_stream(
            _metadata(" EN_us "), _audio_stream(b"\x00\x01")
        )
    finally:
        chat_session.current_session.reset(session_context)

    assert result.result is stt.SpeechResultState.SUCCESS
    assert stream_transcribe.await_args is not None
    handoff = stream_transcribe.await_args.kwargs["speech_session_handoff"]
    assert handoff.client is client
    assert handoff.session is session
    assert handoff.voice == "ember"
    assert handoff.language == "en-US"
    assert stream_transcribe.await_args.args[1] == _bridge_metadata()


async def test_stt_active_session_without_pipeline_preparation_stays_cold() -> None:
    """A ChatSession and configured TTS voice alone cannot opt STT into reuse."""
    stream_transcribe = AsyncMock(return_value="Cold transcript")
    client = SimpleNamespace(
        async_transcribe_stream=stream_transcribe,
        async_transcribe=AsyncMock(),
    )
    tts_subentry = ConfigSubentry(
        data=MappingProxyType({CONF_VOICE: "ember"}),
        subentry_type="tts",
        title="Test TTS",
        unique_id=None,
    )
    entity = _make_entity(
        client,
        entry_subentries={tts_subentry.subentry_id: tts_subentry},
    )
    session_context = chat_session.current_session.set(
        chat_session.ChatSession("assist-session")
    )
    try:
        result = await entity.async_process_audio_stream(
            _metadata(), _audio_stream(b"\x00\x01")
        )
    finally:
        chat_session.current_session.reset(session_context)

    assert result.result is stt.SpeechResultState.SUCCESS
    assert stream_transcribe.await_args is not None
    assert "speech_session_handoff" not in stream_transcribe.await_args.kwargs


async def test_pipeline_tts_options_leave_handoff_disabled_by_default() -> None:
    """An eligible Assist context still uses isolated STT and TTS sessions."""
    stream_transcribe = AsyncMock(return_value="Cold transcript")
    client = SimpleNamespace(
        async_transcribe_stream=stream_transcribe,
        async_transcribe=AsyncMock(),
    )
    tts_subentry = ConfigSubentry(
        data=MappingProxyType({CONF_VOICE: "ember"}),
        subentry_type="tts",
        title="Test TTS",
        unique_id=None,
    )
    tts_entity = CodexVoiceTTSEntity(_make_entry(client), tts_subentry)
    stt_entity = _make_entity(client)
    session_context = chat_session.current_session.set(
        chat_session.ChatSession("assist-session")
    )
    try:
        pipeline_options = dict(tts_entity.default_options)
        result = await stt_entity.async_process_audio_stream(
            _metadata(), _audio_stream(b"\x00\x01")
        )
    finally:
        chat_session.current_session.reset(session_context)

    assert api_module._SPEECH_SESSION_HANDOFF_OPTION not in pipeline_options
    assert result.result is stt.SpeechResultState.SUCCESS
    assert stream_transcribe.await_args is not None
    assert "speech_session_handoff" not in stream_transcribe.await_args.kwargs


async def test_instruction_configured_tts_does_not_prepare_stt_handoff() -> None:
    """A synthesis profile requiring instructions stays cold from preparation."""
    stream_transcribe = AsyncMock(return_value="Cold transcript")
    client = SimpleNamespace(
        async_transcribe_stream=stream_transcribe,
        async_transcribe=AsyncMock(),
    )
    tts_subentry = ConfigSubentry(
        data=MappingProxyType(
            {
                CONF_VOICE: "ember",
                CONF_INSTRUCTIONS: "Speak quietly",
            }
        ),
        subentry_type="tts",
        title="Instruction TTS",
        unique_id=None,
    )
    tts_entity = CodexVoiceTTSEntity(_make_entry(client), tts_subentry)
    stt_entity = _make_entity(client)
    session_context = chat_session.current_session.set(
        chat_session.ChatSession("assist-session")
    )
    try:
        assert (
            api_module._SPEECH_SESSION_HANDOFF_OPTION not in tts_entity.default_options
        )
        result = await stt_entity.async_process_audio_stream(
            _metadata(), _audio_stream(b"\x00\x01")
        )
    finally:
        chat_session.current_session.reset(session_context)

    assert result.result is stt.SpeechResultState.SUCCESS
    assert stream_transcribe.await_args is not None
    assert "speech_session_handoff" not in stream_transcribe.await_args.kwargs


async def test_stt_returns_standard_error_for_blank_result() -> None:
    """A blank streaming transcript returns the standard STT error state."""
    stream_transcribe = AsyncMock(return_value="  ")
    finite_transcribe = AsyncMock()
    entity = _make_entity(
        SimpleNamespace(
            async_transcribe_stream=stream_transcribe,
            async_transcribe=finite_transcribe,
        )
    )

    result = await entity.async_process_audio_stream(_metadata(), _audio_stream())

    assert result.result is stt.SpeechResultState.ERROR
    assert result.text is None
    stream_transcribe.assert_awaited_once()
    finite_transcribe.assert_not_awaited()


async def test_stt_falls_back_without_prior_stream_consumption() -> None:
    """A pre-upgrade unsupported response safely reuses the untouched iterable."""
    yielded_chunks = 0
    streaming_handoff: Any = None

    async def audio_stream() -> AsyncIterator[bytes]:
        nonlocal yielded_chunks
        for chunk in (b"\x00\x01", b"\x02\x03"):
            yielded_chunks += 1
            yield chunk

    async def stream_transcribe(
        stream: AsyncIterator[bytes],
        metadata: dict[str, Any],
        *,
        prompt: str | None,
        speech_session_handoff: Any,
    ) -> str:
        nonlocal streaming_handoff
        assert yielded_chunks == 0
        streaming_handoff = speech_session_handoff
        raise BridgeStreamingUnsupported

    finite_transcribe = AsyncMock(return_value="Legacy result")
    client = SimpleNamespace(
        async_transcribe_stream=AsyncMock(side_effect=stream_transcribe),
        async_transcribe=finite_transcribe,
    )
    entity = _make_entity(client, data={CONF_PROMPT: "Short commands"})
    session_context = chat_session.current_session.set(
        chat_session.ChatSession("assist-session")
    )
    try:
        assert api_module._prepare_speech_session_handoff(
            cast("BridgeClient", client),
            voice="cove",
        )
        result = await entity.async_process_audio_stream(_metadata(), audio_stream())
    finally:
        chat_session.current_session.reset(session_context)

    assert result.result is stt.SpeechResultState.SUCCESS
    assert result.text == "Legacy result"
    assert yielded_chunks == 2
    finite_transcribe.assert_awaited_once_with(
        b"\x00\x01\x02\x03",
        _bridge_metadata(),
        prompt="Short commands",
        speech_session_handoff=streaming_handoff,
    )


async def test_stt_bounds_legacy_fallback_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy fallback does not collect or POST audio beyond the input cap."""
    monkeypatch.setattr(stt_module, "MAX_AUDIO_BYTES", 3)
    finite_transcribe = AsyncMock()
    entity = _make_entity(
        SimpleNamespace(
            async_transcribe_stream=AsyncMock(side_effect=BridgeStreamingUnsupported),
            async_transcribe=finite_transcribe,
        )
    )

    result = await entity.async_process_audio_stream(
        _metadata(), _audio_stream(b"123", b"4")
    )

    assert result.result is stt.SpeechResultState.ERROR
    finite_transcribe.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "reauth_expected"),
    [
        (BridgeAuthenticationError("private authentication detail"), True),
        (BridgeBusyError("private busy detail"), False),
        (BridgeQuotaError("private quota detail"), False),
        (BridgeConnectionError("private server detail"), False),
        (BridgeProtocolError("private protocol detail"), False),
    ],
)
async def test_stt_never_falls_back_for_streaming_errors(
    error: Exception,
    reauth_expected: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only authentication starts reauth and no upgraded failure uses finite POST."""
    finite_transcribe = AsyncMock()
    client = SimpleNamespace(
        async_transcribe_stream=AsyncMock(side_effect=error),
        async_transcribe=finite_transcribe,
    )
    entity = _make_entity(client, data={CONF_PROMPT: "private prompt"})

    with caplog.at_level(logging.ERROR):
        result = await entity.async_process_audio_stream(
            _metadata(), _audio_stream(b"private audio")
        )

    assert result.result is stt.SpeechResultState.ERROR
    finite_transcribe.assert_not_awaited()
    start_reauth = cast("Mock", entity.entry.async_start_reauth)
    if reauth_expected:
        start_reauth.assert_called_once_with(entity.hass)
    else:
        start_reauth.assert_not_called()
    assert "private" not in caplog.text


async def test_stt_cancellation_propagates() -> None:
    """Cancelling streaming transcription is not converted into an STT error."""
    started = asyncio.Event()

    async def stream_transcribe(*args: Any, **kwargs: Any) -> str:
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    entity = _make_entity(
        SimpleNamespace(
            async_transcribe_stream=stream_transcribe,
            async_transcribe=AsyncMock(),
        )
    )
    task = asyncio.create_task(
        entity.async_process_audio_stream(_metadata(), _audio_stream(b"audio"))
    )

    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
