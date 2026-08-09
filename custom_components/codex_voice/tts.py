"""Text-to-speech provider for Codex Voice."""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from collections.abc import AsyncGenerator, Mapping
from typing import Any, Final, override

from homeassistant.components.tts import (
    ATTR_PREFERRED_FORMAT,
    ATTR_PREFERRED_SAMPLE_BYTES,
    ATTR_PREFERRED_SAMPLE_CHANNELS,
    ATTR_PREFERRED_SAMPLE_RATE,
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
from .api import (
    _SPEECH_SESSION_HANDOFF_OPTION,
    _SPEECH_SESSION_HANDOFF_OPTION_VALUE,
    BridgeAudio,
    BridgeAuthenticationError,
    BridgeError,
    BridgeProtocolError,
    _claim_speech_session_handoff,
    _normalize_speech_language,
    _prepare_speech_session_handoff,
    _schedule_speech_session_handoff_release,
    _validate_synthesis_audio_preferences,
)
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

# Frameless Bidi v3 currently begins assistant output before finite STT has
# completed, and its supported client protocol has no response-cancel control.
# Keep the private reuse machinery dormant until a quiet handoff can be proven.
_AUTOMATIC_SPEECH_SESSION_HANDOFF_ENABLED: Final = False

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

    _attr_supported_options = [
        ATTR_VOICE,
        ATTR_PREFERRED_FORMAT,
        ATTR_PREFERRED_SAMPLE_RATE,
        ATTR_PREFERRED_SAMPLE_CHANNELS,
        ATTR_PREFERRED_SAMPLE_BYTES,
    ]
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
        voice = self.subentry.data.get(CONF_VOICE, DEFAULT_VOICE)
        options: dict[str, Any] = {
            ATTR_VOICE: voice,
            ATTR_PREFERRED_FORMAT: "wav",
        }
        if (
            _AUTOMATIC_SPEECH_SESSION_HANDOFF_ENABLED
            and not self.subentry.data.get(CONF_INSTRUCTIONS)
            and _prepare_speech_session_handoff(
                self.entry.runtime_data,
                voice=voice,
            )
        ):
            options[_SPEECH_SESSION_HANDOFF_OPTION] = (
                _SPEECH_SESSION_HANDOFF_OPTION_VALUE
            )
        return options

    @override
    async def async_get_tts_audio(
        self,
        message: str,
        language: str,
        options: dict[str, Any],
    ) -> TtsAudioType:
        """Synthesize a message through the Codex Voice bridge."""
        merged_options = {**self.default_options, **self.subentry.data, **options}
        audio_preferences = _synthesis_audio_preferences(merged_options)
        client = self.entry.runtime_data
        language = _normalize_speech_language(language)
        voice = merged_options.get(ATTR_VOICE, DEFAULT_VOICE)
        instructions = merged_options.get(CONF_INSTRUCTIONS)
        handoff_token = _claim_speech_session_handoff(
            client,
            language=language,
            voice=voice,
            instructions=instructions,
            options=merged_options,
        )
        synthesize_kwargs = {
            "language": language,
            "voice": voice,
            "instructions": instructions,
            **audio_preferences,
        }
        if handoff_token is not None:
            synthesize_kwargs["speech_session_handoff_token"] = handoff_token
        try:
            audio = await client.async_synthesize(
                message,
                **synthesize_kwargs,
            )
        except asyncio.CancelledError:
            if handoff_token is not None:
                _schedule_speech_session_handoff_release(client, handoff_token)
            raise
        except BridgeAuthenticationError as err:
            if handoff_token is not None:
                _schedule_speech_session_handoff_release(client, handoff_token)
            self.entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain="codex_voice",
                translation_key="authentication_required",
            ) from err
        except BridgeError as err:
            if handoff_token is not None:
                _schedule_speech_session_handoff_release(client, handoff_token)
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
        merged_options = {
            **self.default_options,
            **self.subentry.data,
            **request.options,
        }
        audio_preferences = _synthesis_audio_preferences(merged_options)
        client = self.entry.runtime_data
        language = _normalize_speech_language(request.language)
        voice = merged_options.get(ATTR_VOICE, DEFAULT_VOICE)
        instructions = merged_options.get(CONF_INSTRUCTIONS)
        handoff_token = _claim_speech_session_handoff(
            client,
            language=language,
            voice=voice,
            instructions=instructions,
            options=merged_options,
        )
        message_collected = False
        try:
            message = "".join([chunk async for chunk in request.message_gen])
            message_collected = True
        finally:
            if not message_collected and handoff_token is not None:
                _schedule_speech_session_handoff_release(client, handoff_token)

        synthesize_kwargs = {
            "language": language,
            "voice": voice,
            "instructions": instructions,
            **audio_preferences,
        }
        if handoff_token is not None:
            synthesize_kwargs["speech_session_handoff_token"] = handoff_token
        stream_created = False
        try:
            bridge_stream = client.async_synthesize_stream(
                message,
                **synthesize_kwargs,
            )
            stream_created = True
        finally:
            if not stream_created and handoff_token is not None:
                _schedule_speech_session_handoff_release(client, handoff_token)

        async def data_gen() -> AsyncGenerator[bytes]:
            completed = False
            try:
                async for chunk in bridge_stream:
                    yield chunk
                completed = True
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
                try:
                    await bridge_stream.aclose()
                finally:
                    if not completed and handoff_token is not None:
                        _schedule_speech_session_handoff_release(client, handoff_token)

        return TTSAudioResponse(extension="wav", data_gen=data_gen())


def _synthesis_audio_preferences(options: Mapping[str, Any]) -> dict[str, int]:
    """Translate Home Assistant output hints to validated bridge options."""
    preferred_format = options.get(ATTR_PREFERRED_FORMAT, "wav")
    if not isinstance(preferred_format, str) or preferred_format.lower() != "wav":
        return {}

    requested_preferences = (
        options.get(ATTR_PREFERRED_SAMPLE_RATE),
        options.get(ATTR_PREFERRED_SAMPLE_CHANNELS),
        options.get(ATTR_PREFERRED_SAMPLE_BYTES),
    )
    if any(value is None for value in requested_preferences):
        return {}

    try:
        sample_rate, channels, sample_width = _validate_synthesis_audio_preferences(
            sample_rate=requested_preferences[0],
            channels=requested_preferences[1],
            sample_width=requested_preferences[2],
        )
    except BridgeProtocolError:
        # HA can request other satellite-native layouts. Leaving the bridge
        # fields absent preserves its 24 kHz mono PCM16 default and lets HA's
        # TTS manager apply its normal conversion fallback.
        return {}

    assert sample_rate is not None
    assert channels is not None
    assert sample_width is not None

    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width": sample_width,
    }


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
