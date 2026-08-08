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

from custom_components.codex_voice import CodexVoiceConfigEntry
from custom_components.codex_voice import stt as stt_module
from custom_components.codex_voice.api import (
    BridgeAuthenticationError,
    BridgeBusyError,
    BridgeConnectionError,
    BridgeProtocolError,
    BridgeQuotaError,
    BridgeStreamingUnsupported,
)
from custom_components.codex_voice.stt import CodexVoiceSTTEntity


async def _audio_stream(*chunks: bytes) -> AsyncIterator[bytes]:
    """Yield test audio chunks."""
    for chunk in chunks:
        yield chunk


def _make_entry(client: Any) -> CodexVoiceConfigEntry:
    """Create the minimal config-entry interface required by an entity."""
    return cast(
        "CodexVoiceConfigEntry",
        SimpleNamespace(runtime_data=client, async_start_reauth=Mock()),
    )


def _make_entity(
    client: Any, *, data: dict[str, Any] | None = None
) -> CodexVoiceSTTEntity:
    """Create an STT entity with a minimal config subentry."""
    entry = _make_entry(client)
    subentry = ConfigSubentry(
        data=MappingProxyType(data or {}),
        subentry_type="stt",
        title="Test STT",
        unique_id=None,
    )
    return CodexVoiceSTTEntity(entry, subentry)


def _metadata() -> stt.SpeechMetadata:
    """Return supported mono PCM metadata."""
    return stt.SpeechMetadata(
        language="en-US",
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
    ) -> str:
        assert yielded_chunks == 0
        raise BridgeStreamingUnsupported

    finite_transcribe = AsyncMock(return_value="Legacy result")
    client = SimpleNamespace(
        async_transcribe_stream=AsyncMock(side_effect=stream_transcribe),
        async_transcribe=finite_transcribe,
    )
    entity = _make_entity(client, data={CONF_PROMPT: "Short commands"})

    result = await entity.async_process_audio_stream(_metadata(), audio_stream())

    assert result.result is stt.SpeechResultState.SUCCESS
    assert result.text == "Legacy result"
    assert yielded_chunks == 2
    finite_transcribe.assert_awaited_once_with(
        b"\x00\x01\x02\x03",
        _bridge_metadata(),
        prompt="Short commands",
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
