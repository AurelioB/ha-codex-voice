"""Tests for the Codex Voice text-to-speech entity."""

from __future__ import annotations

import asyncio
import io
import wave
from collections.abc import AsyncGenerator
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.tts import ATTR_PREFERRED_FORMAT, TTSAudioRequest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.exceptions import HomeAssistantError

from custom_components.codex_voice import CodexVoiceConfigEntry
from custom_components.codex_voice.api import (
    BridgeAudio,
    BridgeAuthenticationError,
    BridgeProtocolError,
)
from custom_components.codex_voice.const import CONF_INSTRUCTIONS, CONF_VOICE
from custom_components.codex_voice.tts import CodexVoiceTTSEntity


def _make_entry(client: Any) -> CodexVoiceConfigEntry:
    """Create the minimal config-entry interface required by an entity."""
    return cast(
        "CodexVoiceConfigEntry",
        SimpleNamespace(runtime_data=client, async_start_reauth=Mock()),
    )


async def _message_chunks(*chunks: str) -> AsyncGenerator[str]:
    """Yield message fragments like Home Assistant's streaming pipeline."""
    for chunk in chunks:
        yield chunk


async def test_tts_wraps_pcm_in_wav() -> None:
    """JSON PCM from the bridge becomes a playable WAV response."""
    synthesize = AsyncMock(
        return_value=BridgeAudio(
            data=b"\x00\x01\x02\x03",
            audio_format="pcm",
            sample_rate=24000,
            channels=1,
            sample_width=2,
        )
    )
    entry = _make_entry(SimpleNamespace(async_synthesize=synthesize))
    subentry = ConfigSubentry(
        data=MappingProxyType({CONF_VOICE: "cove"}),
        subentry_type="tts",
        title="Test TTS",
        unique_id=None,
    )
    entity = CodexVoiceTTSEntity(entry, subentry)

    audio_format, audio_data = await entity.async_get_tts_audio(
        "Hello",
        "en-US",
        {},
    )

    assert audio_format == "wav"
    assert audio_data is not None
    with wave.open(io.BytesIO(audio_data), "rb") as wav_file:
        assert wav_file.getframerate() == 24000
        assert wav_file.getnchannels() == 1
        assert wav_file.readframes(2) == b"\x00\x01\x02\x03"
    synthesize.assert_awaited_once_with(
        "Hello",
        language="en-US",
        voice="cove",
        instructions=None,
    )


async def test_tts_streams_bridge_audio_and_merges_options() -> None:
    """The entity forwards collected text and progressively yields bridge audio."""
    first_audio_released = asyncio.Event()
    release_second_audio = asyncio.Event()

    async def audio_stream() -> AsyncGenerator[bytes]:
        first_audio_released.set()
        yield b"RIFF-stream-header"
        await release_second_audio.wait()
        yield b"audio"

    synthesize_stream = Mock(return_value=audio_stream())
    entry = _make_entry(SimpleNamespace(async_synthesize_stream=synthesize_stream))
    subentry = ConfigSubentry(
        data=MappingProxyType(
            {CONF_VOICE: "cove", CONF_INSTRUCTIONS: "Subentry instructions"}
        ),
        subentry_type="tts",
        title="Test TTS",
        unique_id=None,
    )
    entity = CodexVoiceTTSEntity(entry, subentry)

    response = await entity.async_stream_tts_audio(
        TTSAudioRequest(
            language="en-US",
            options={
                CONF_VOICE: "ember",
                CONF_INSTRUCTIONS: "Request instructions",
            },
            message_gen=_message_chunks("Hel", "lo"),
        )
    )

    assert entity.async_supports_streaming_input()
    assert ATTR_PREFERRED_FORMAT not in entity.supported_options
    assert entity.default_options[ATTR_PREFERRED_FORMAT] == "wav"
    assert response.extension == "wav"
    first_chunk = await anext(response.data_gen)
    await asyncio.wait_for(first_audio_released.wait(), timeout=1)
    assert first_chunk == b"RIFF-stream-header"
    release_second_audio.set()
    assert [chunk async for chunk in response.data_gen] == [b"audio"]
    synthesize_stream.assert_called_once_with(
        "Hello",
        language="en-US",
        voice="ember",
        instructions="Request instructions",
    )


async def test_tts_stream_defers_authentication_error_and_starts_reauth() -> None:
    """Authentication failures raised during consumption trigger reauthentication."""

    async def audio_stream() -> AsyncGenerator[bytes]:
        yield b"RIFF-stream-header"
        raise BridgeAuthenticationError("token rejected")

    client = SimpleNamespace(async_synthesize_stream=Mock(return_value=audio_stream()))
    entry = _make_entry(client)
    subentry = ConfigSubentry(
        data=MappingProxyType({CONF_VOICE: "cove"}),
        subentry_type="tts",
        title="Test TTS",
        unique_id=None,
    )
    entity = CodexVoiceTTSEntity(entry, subentry)
    entity.hass = cast("Any", object())
    response = await entity.async_stream_tts_audio(
        TTSAudioRequest("en-US", {}, _message_chunks("Hello"))
    )

    with pytest.raises(HomeAssistantError) as caught:
        _ = [chunk async for chunk in response.data_gen]

    assert caught.value.translation_key == "authentication_required"
    entry.async_start_reauth.assert_called_once_with(entity.hass)


async def test_tts_stream_error_log_does_not_include_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Neither request text nor bridge error details leak into synthesis logs."""
    private_message = "private spoken request 7429"

    async def audio_stream() -> AsyncGenerator[bytes]:
        yield b"RIFF-stream-header"
        raise BridgeProtocolError(private_message)

    client = SimpleNamespace(async_synthesize_stream=Mock(return_value=audio_stream()))
    entry = _make_entry(client)
    subentry = ConfigSubentry(
        data=MappingProxyType({CONF_VOICE: "cove"}),
        subentry_type="tts",
        title="Test TTS",
        unique_id=None,
    )
    entity = CodexVoiceTTSEntity(entry, subentry)
    response = await entity.async_stream_tts_audio(
        TTSAudioRequest("en-US", {}, _message_chunks(private_message))
    )

    with pytest.raises(HomeAssistantError) as caught:
        _ = [chunk async for chunk in response.data_gen]

    assert caught.value.translation_key == "synthesis_failed"
    assert private_message not in caplog.text


async def test_tts_stream_closes_bridge_stream_when_consumer_stops() -> None:
    """Closing Home Assistant's stream promptly propagates to the HTTP stream."""
    bridge_stream_closed = asyncio.Event()

    async def audio_stream() -> AsyncGenerator[bytes]:
        try:
            yield b"RIFF-stream-header"
            await asyncio.Event().wait()
        finally:
            bridge_stream_closed.set()

    client = SimpleNamespace(async_synthesize_stream=Mock(return_value=audio_stream()))
    entry = _make_entry(client)
    subentry = ConfigSubentry(
        data=MappingProxyType({CONF_VOICE: "cove"}),
        subentry_type="tts",
        title="Test TTS",
        unique_id=None,
    )
    entity = CodexVoiceTTSEntity(entry, subentry)
    response = await entity.async_stream_tts_audio(
        TTSAudioRequest("en-US", {}, _message_chunks("Hello"))
    )

    _ = await anext(response.data_gen)
    await response.data_gen.aclose()

    assert bridge_stream_closed.is_set()
