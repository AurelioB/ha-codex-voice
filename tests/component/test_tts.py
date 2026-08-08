"""Tests for the Codex Voice text-to-speech entity."""

from __future__ import annotations

import io
import wave
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from homeassistant.config_entries import ConfigSubentry

from custom_components.codex_voice import CodexVoiceConfigEntry
from custom_components.codex_voice.api import BridgeAudio
from custom_components.codex_voice.const import CONF_VOICE
from custom_components.codex_voice.tts import CodexVoiceTTSEntity


def _make_entry(client: Any) -> CodexVoiceConfigEntry:
    """Create the minimal config-entry interface required by an entity."""
    return cast(
        "CodexVoiceConfigEntry",
        SimpleNamespace(runtime_data=client, async_start_reauth=Mock()),
    )


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
