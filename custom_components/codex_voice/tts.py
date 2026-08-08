"""Text-to-speech provider for Codex Voice."""

from __future__ import annotations

import io
import logging
import wave
from collections.abc import AsyncGenerator, Mapping
from typing import Any, override

from homeassistant.components.tts import (
    ATTR_PREFERRED_FORMAT,
    ATTR_VOICE,
    TextToSpeechEntity,
    TTSAudioRequest,
    TTSAudioResponse,
    TtsAudioType,
    Voice,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CodexVoiceConfigEntry
from .api import BridgeAudio, BridgeAuthenticationError, BridgeError
from .const import (
    CONF_INSTRUCTIONS,
    CONF_VOICE,
    DEFAULT_VOICE,
    SUBENTRY_TYPE_TTS,
    SUPPORTED_LANGUAGES,
    SUPPORTED_VOICES,
)
from .entity import CodexVoiceEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CodexVoiceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Codex Voice TTS entities."""
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_TTS:
            continue
        async_add_entities(
            [CodexVoiceTTSEntity(config_entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class CodexVoiceTTSEntity(TextToSpeechEntity, CodexVoiceEntity):
    """Codex Voice text-to-speech entity."""

    _attr_supported_options = [ATTR_VOICE]
    _attr_supported_languages = list(SUPPORTED_LANGUAGES)
    _attr_default_language = "en-US"
    _attr_has_entity_name = False
    _attr_translation_key = "tts"
    _supported_voices = [Voice(voice, voice.title()) for voice in SUPPORTED_VOICES]

    def __init__(
        self,
        entry: CodexVoiceConfigEntry,
        subentry: ConfigSubentry,
    ) -> None:
        """Initialize the TTS entity."""
        super().__init__(entry, subentry)
        self._attr_name = subentry.title

    @callback
    @override
    def async_get_supported_voices(self, language: str) -> list[Voice]:
        """Return supported voices for a language."""
        return self._supported_voices

    @property
    @override
    def default_options(self) -> Mapping[str, Any]:
        """Return default synthesis options."""
        return {
            ATTR_VOICE: self.subentry.data.get(CONF_VOICE, DEFAULT_VOICE),
            ATTR_PREFERRED_FORMAT: "wav",
        }

    @override
    async def async_get_tts_audio(
        self,
        message: str,
        language: str,
        options: dict[str, Any],
    ) -> TtsAudioType:
        """Synthesize a message through the Codex Voice bridge."""
        merged_options = {**self.default_options, **self.subentry.data, **options}
        try:
            audio = await self.entry.runtime_data.async_synthesize(
                message,
                language=language,
                voice=merged_options.get(ATTR_VOICE, DEFAULT_VOICE),
                instructions=merged_options.get(CONF_INSTRUCTIONS),
            )
        except BridgeAuthenticationError as err:
            self.entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain="codex_voice",
                translation_key="authentication_required",
            ) from err
        except BridgeError as err:
            _LOGGER.error(
                "Error synthesizing speech with Codex Voice (%s)",
                type(err).__name__,
            )
            raise HomeAssistantError(
                translation_domain="codex_voice",
                translation_key="synthesis_failed",
            ) from err

        return "wav", _as_wav(audio)

    @override
    async def async_stream_tts_audio(
        self,
        request: TTSAudioRequest,
    ) -> TTSAudioResponse:
        """Stream synthesized WAV audio through the Codex Voice bridge."""
        message = "".join([chunk async for chunk in request.message_gen])
        merged_options = {
            **self.default_options,
            **self.subentry.data,
            **request.options,
        }
        bridge_stream = self.entry.runtime_data.async_synthesize_stream(
            message,
            language=request.language,
            voice=merged_options.get(ATTR_VOICE, DEFAULT_VOICE),
            instructions=merged_options.get(CONF_INSTRUCTIONS),
        )

        async def data_gen() -> AsyncGenerator[bytes]:
            try:
                async for chunk in bridge_stream:
                    yield chunk
            except BridgeAuthenticationError as err:
                self.entry.async_start_reauth(self.hass)
                raise HomeAssistantError(
                    translation_domain="codex_voice",
                    translation_key="authentication_required",
                ) from err
            except BridgeError as err:
                _LOGGER.error(
                    "Error streaming speech with Codex Voice (%s)",
                    type(err).__name__,
                )
                raise HomeAssistantError(
                    translation_domain="codex_voice",
                    translation_key="synthesis_failed",
                ) from err
            finally:
                await bridge_stream.aclose()

        return TTSAudioResponse(extension="wav", data_gen=data_gen())


def _as_wav(audio: BridgeAudio) -> bytes:
    """Return bridge audio as a valid WAV file."""
    if audio.audio_format in ("wav", "wave"):
        return audio.data
    if audio.audio_format not in ("pcm", "raw"):
        raise HomeAssistantError(
            translation_domain="codex_voice",
            translation_key="unsupported_audio_format",
            translation_placeholders={"audio_format": audio.audio_format},
        )

    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(audio.channels)
        wav_file.setsampwidth(audio.sample_width)
        wav_file.setframerate(audio.sample_rate)
        wav_file.writeframes(audio.data)
    return output.getvalue()
