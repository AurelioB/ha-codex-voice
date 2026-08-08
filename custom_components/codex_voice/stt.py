"""Speech-to-text provider for Codex Voice."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable
from typing import override

from homeassistant.components import stt
from homeassistant.const import CONF_PROMPT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CodexVoiceConfigEntry
from .api import BridgeAuthenticationError, BridgeError
from .const import MAX_AUDIO_BYTES, SUBENTRY_TYPE_STT, SUPPORTED_LANGUAGES
from .entity import CodexVoiceEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: CodexVoiceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Codex Voice STT entities."""
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_STT:
            continue
        async_add_entities(
            [CodexVoiceSTTEntity(config_entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class CodexVoiceSTTEntity(stt.SpeechToTextEntity, CodexVoiceEntity):
    """Codex Voice speech-to-text entity."""

    _attr_translation_key = "stt"

    @property
    @override
    def supported_languages(self) -> list[str]:
        """Return supported languages."""
        return list(SUPPORTED_LANGUAGES)

    @property
    @override
    def supported_formats(self) -> list[stt.AudioFormats]:
        """Return supported stream formats."""
        return [stt.AudioFormats.WAV]

    @property
    @override
    def supported_codecs(self) -> list[stt.AudioCodecs]:
        """Return supported codecs."""
        return [stt.AudioCodecs.PCM]

    @property
    @override
    def supported_bit_rates(self) -> list[stt.AudioBitRates]:
        """Return supported PCM sample widths."""
        return [stt.AudioBitRates.BITRATE_16]

    @property
    @override
    def supported_sample_rates(self) -> list[stt.AudioSampleRates]:
        """Return supported input sample rates."""
        return [
            stt.AudioSampleRates.SAMPLERATE_16000,
            stt.AudioSampleRates.SAMPLERATE_48000,
        ]

    @property
    @override
    def supported_channels(self) -> list[stt.AudioChannels]:
        """Return supported channels."""
        return [stt.AudioChannels.CHANNEL_MONO]

    @override
    async def async_process_audio_stream(
        self,
        metadata: stt.SpeechMetadata,
        stream: AsyncIterable[bytes],
    ) -> stt.SpeechResult:
        """Collect bounded PCM and transcribe it through the bridge."""
        audio = bytearray()
        async for chunk in stream:
            if len(audio) + len(chunk) > MAX_AUDIO_BYTES:
                _LOGGER.warning("Rejecting STT input larger than 16 MiB")
                return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
            audio.extend(chunk)

        if not audio:
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)

        bridge_metadata = {
            "language": metadata.language,
            "codec": metadata.codec.value,
            "sample_rate": metadata.sample_rate.value,
            "bit_rate": metadata.bit_rate.value,
            "channels": metadata.channel.value,
        }
        try:
            transcript = await self.entry.runtime_data.async_transcribe(
                bytes(audio),
                bridge_metadata,
                prompt=self.subentry.data.get(CONF_PROMPT),
            )
        except BridgeAuthenticationError:
            self.entry.async_start_reauth(self.hass)
            _LOGGER.error("The Codex Voice bridge requires reauthentication")
        except BridgeError:
            _LOGGER.exception("Error transcribing speech with Codex Voice")
        else:
            if transcript.strip():
                return stt.SpeechResult(
                    transcript,
                    stt.SpeechResultState.SUCCESS,
                )

        return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
