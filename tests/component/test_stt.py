"""Tests for the Codex Voice speech-to-text entity."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from homeassistant.components import stt
from homeassistant.config_entries import ConfigSubentry

from custom_components.codex_voice import CodexVoiceConfigEntry
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


async def test_stt_forwards_pcm_and_metadata() -> None:
    """The STT entity combines chunks and forwards standard metadata."""
    transcribe = AsyncMock(return_value="Turn on the kitchen")
    entry = _make_entry(SimpleNamespace(async_transcribe=transcribe))
    subentry = ConfigSubentry(
        data=MappingProxyType({}),
        subentry_type="stt",
        title="Test STT",
        unique_id=None,
    )
    entity = CodexVoiceSTTEntity(entry, subentry)

    result = await entity.async_process_audio_stream(
        _metadata(),
        _audio_stream(b"\x00\x01", b"\x02\x03"),
    )

    assert result.result is stt.SpeechResultState.SUCCESS
    assert result.text == "Turn on the kitchen"
    transcribe.assert_awaited_once_with(
        b"\x00\x01\x02\x03",
        {
            "language": "en-US",
            "codec": "pcm",
            "sample_rate": 16000,
            "bit_rate": 16,
            "channels": 1,
        },
        prompt=None,
    )


async def test_stt_rejects_empty_audio() -> None:
    """An empty audio stream returns the standard STT error state."""
    transcribe = AsyncMock()
    entry = _make_entry(SimpleNamespace(async_transcribe=transcribe))
    subentry = ConfigSubentry(
        data=MappingProxyType({}),
        subentry_type="stt",
        title="Test STT",
        unique_id=None,
    )
    entity = CodexVoiceSTTEntity(entry, subentry)

    result = await entity.async_process_audio_stream(_metadata(), _audio_stream())

    assert result.result is stt.SpeechResultState.ERROR
    transcribe.assert_not_awaited()
