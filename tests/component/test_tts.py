"""Tests for the Codex Voice text-to-speech entity."""

from __future__ import annotations

import asyncio
import io
import wave
from collections.abc import AsyncGenerator
from contextvars import copy_context
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.tts import (
    ATTR_PREFERRED_FORMAT,
    ATTR_PREFERRED_SAMPLE_BYTES,
    ATTR_PREFERRED_SAMPLE_CHANNELS,
    ATTR_PREFERRED_SAMPLE_RATE,
    TTSAudioRequest,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import chat_session

from custom_components.codex_voice import CodexVoiceConfigEntry
from custom_components.codex_voice import api as api_module
from custom_components.codex_voice import tts as tts_module
from custom_components.codex_voice.api import (
    BridgeAudio,
    BridgeAuthenticationError,
    BridgeClient,
    BridgeProtocolError,
)
from custom_components.codex_voice.const import CONF_INSTRUCTIONS, CONF_VOICE
from custom_components.codex_voice.tts import CodexVoiceTTSEntity

_TEST_HANDOFF_TOKEN = "opaque-ticket"


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


def _make_tts_entity(
    client: Any,
    *,
    voice: str = "cove",
    instructions: str | None = None,
) -> CodexVoiceTTSEntity:
    """Create a minimal TTS entity for handoff tests."""
    data: dict[str, Any] = {CONF_VOICE: voice}
    if instructions is not None:
        data[CONF_INSTRUCTIONS] = instructions
    return CodexVoiceTTSEntity(
        _make_entry(client),
        ConfigSubentry(
            data=MappingProxyType(data),
            subentry_type="tts",
            title="Test TTS",
            unique_id=None,
        ),
    )


def test_tts_advertises_mexican_spanish() -> None:
    """Expose the BCP-47 locale used by Mexican Spanish Assist pipelines."""
    entity = _make_tts_entity(AsyncMock())

    assert "es-MX" in entity.supported_languages


def _offer_handoff(
    client: Any,
    *,
    token: str | None = None,
    voice: str = "cove",
    language: str = "en-US",
    expires_in_ms: int = 30_000,
) -> dict[str, Any]:
    """Privately install a bridge result ticket in the current Assist context."""
    if token is None:
        token = _TEST_HANDOFF_TOKEN
    bridge_client = cast("BridgeClient", client)
    assert api_module._prepare_speech_session_handoff(bridge_client, voice=voice)
    options = {
        api_module._SPEECH_SESSION_HANDOFF_OPTION: (
            api_module._SPEECH_SESSION_HANDOFF_OPTION_VALUE
        )
    }
    _store_prepared_handoff(
        client,
        token=token,
        language=language,
        expires_in_ms=expires_in_ms,
    )
    return options


def _store_prepared_handoff(
    client: Any,
    *,
    token: str = _TEST_HANDOFF_TOKEN,
    language: str = "en-US",
    expires_in_ms: int = 30_000,
) -> None:
    """Store a bridge offer after the TTS entity prepared pipeline options."""
    bridge_client = cast("BridgeClient", client)
    request = api_module._begin_speech_session_handoff(
        bridge_client,
        language=language,
    )
    assert request is not None
    api_module._store_pending_speech_session_handoff(
        {
            "speech_session_handoff": {
                "version": 1,
                "token": token,
                "expires_in_ms": expires_in_ms,
                "voice": request.voice,
                "language": request.language,
            }
        },
        request,
    )


async def _wait_for_handoff_releases() -> None:
    """Wait for private best-effort release tasks started by a test."""
    while api_module._HANDOFF_RELEASE_TASKS:
        await asyncio.gather(*tuple(api_module._HANDOFF_RELEASE_TASKS))
        await asyncio.sleep(0)


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


async def test_tts_forwards_native_audio_preferences() -> None:
    """HA's preferred WAV shape is validated and forwarded to the bridge."""
    synthesize = AsyncMock(
        return_value=BridgeAudio(
            data=b"\x00\x01\x02\x03",
            audio_format="pcm",
            sample_rate=16000,
            channels=1,
            sample_width=2,
        )
    )
    entity = _make_tts_entity(SimpleNamespace(async_synthesize=synthesize))

    audio_format, audio_data = await entity.async_get_tts_audio(
        "Hello",
        "en-US",
        {
            ATTR_PREFERRED_FORMAT: "wav",
            ATTR_PREFERRED_SAMPLE_RATE: "16000",
            ATTR_PREFERRED_SAMPLE_CHANNELS: "1",
            ATTR_PREFERRED_SAMPLE_BYTES: "2",
        },
    )

    assert audio_format == "wav"
    assert audio_data is not None
    with wave.open(io.BytesIO(audio_data), "rb") as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
    synthesize.assert_awaited_once_with(
        "Hello",
        language="en-US",
        voice="cove",
        instructions=None,
        sample_rate=16000,
        channels=1,
        sample_width=2,
    )


@pytest.mark.parametrize(
    ("option", "value"),
    [
        (ATTR_PREFERRED_SAMPLE_RATE, 22_050),
        (ATTR_PREFERRED_SAMPLE_CHANNELS, 2),
        (ATTR_PREFERRED_SAMPLE_BYTES, 4),
    ],
)
async def test_tts_uses_default_output_for_unsupported_audio_preferences(
    option: str,
    value: int,
) -> None:
    """Unsupported HA output hints retain the bridge's compatible defaults."""
    synthesize = AsyncMock(
        return_value=BridgeAudio(data=b"\x00\x01", audio_format="pcm")
    )
    entity = _make_tts_entity(SimpleNamespace(async_synthesize=synthesize))

    await entity.async_get_tts_audio(
        "Hello",
        "en-US",
        {
            ATTR_PREFERRED_SAMPLE_RATE: 16000,
            ATTR_PREFERRED_SAMPLE_CHANNELS: 1,
            ATTR_PREFERRED_SAMPLE_BYTES: 2,
            option: value,
        },
    )

    synthesize.assert_awaited_once_with(
        "Hello",
        language="en-US",
        voice="cove",
        instructions=None,
    )


async def test_tts_consumes_same_session_handoff_exactly_once() -> None:
    """Matching finite TTS privately presents a ticket on only its first call."""
    synthesize = AsyncMock(
        return_value=BridgeAudio(data=b"\x00\x01", audio_format="pcm")
    )
    client = SimpleNamespace(
        async_synthesize=synthesize,
        async_release_speech_session_handoff=AsyncMock(),
    )
    entity = _make_tts_entity(client)
    session_context = chat_session.current_session.set(
        chat_session.ChatSession("assist-session")
    )
    try:
        pipeline_options = _offer_handoff(client)
        pending = api_module._PENDING_SPEECH_SESSION_HANDOFF.get()
        assert pending is not None
        assert _TEST_HANDOFF_TOKEN not in repr(pending)
        await entity.async_get_tts_audio("First", "en-US", pipeline_options)
        await entity.async_get_tts_audio("Second", "en-US", pipeline_options)
    finally:
        chat_session.current_session.reset(session_context)

    assert synthesize.await_args_list[0].kwargs == {
        "language": "en-US",
        "voice": "cove",
        "instructions": None,
        "speech_session_handoff_token": "opaque-ticket",
    }
    assert synthesize.await_args_list[1].kwargs == {
        "language": "en-US",
        "voice": "cove",
        "instructions": None,
    }


async def test_pipeline_background_tts_inherits_exact_chat_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA's TTS background task keeps the pipeline context after its parent moves on."""
    monkeypatch.setattr(
        tts_module,
        "_AUTOMATIC_SPEECH_SESSION_HANDOFF_ENABLED",
        True,
    )
    synthesize = AsyncMock(
        return_value=BridgeAudio(data=b"\x00\x01", audio_format="pcm")
    )
    client = SimpleNamespace(
        async_synthesize=synthesize,
        async_release_speech_session_handoff=AsyncMock(),
    )
    entity = _make_tts_entity(client)
    session_context = chat_session.current_session.set(
        chat_session.ChatSession("assist-session")
    )
    try:
        pipeline_options = dict(entity.default_options)
        assert (
            pipeline_options[api_module._SPEECH_SESSION_HANDOFF_OPTION]
            == api_module._SPEECH_SESSION_HANDOFF_OPTION_VALUE
        )
        _store_prepared_handoff(client)
        assert api_module._SPEECH_SESSION_HANDOFF_OPTION not in entity.default_options
        # Home Assistant creates the TTS cache/generation task while the Assist
        # pipeline's ChatSession is active. asyncio copies that ContextVar state
        # even though the parent pipeline can leave the context before audio is
        # actually generated or fetched by the satellite.
        generation = asyncio.create_task(
            entity.async_get_tts_audio("Background", "en-US", pipeline_options)
        )
    finally:
        chat_session.current_session.reset(session_context)

    await generation

    assert synthesize.await_args is not None
    assert (
        synthesize.await_args.kwargs["speech_session_handoff_token"] == "opaque-ticket"
    )


async def test_copied_contexts_cannot_double_claim_handoff() -> None:
    """The shared mutable consumed flag survives ContextVar context copies."""
    client = SimpleNamespace(async_release_speech_session_handoff=AsyncMock())
    session_context = chat_session.current_session.set(
        chat_session.ChatSession("assist-session")
    )
    try:
        pipeline_options = _offer_handoff(client)
        first_context = copy_context()
        second_context = copy_context()

        async def claim() -> str | None:
            return api_module._claim_speech_session_handoff(
                cast("BridgeClient", client),
                language="en-US",
                voice="cove",
                instructions=None,
                options=dict(pipeline_options),
            )

        first = first_context.run(asyncio.create_task, claim())
        second = second_context.run(asyncio.create_task, claim())
        claims = await asyncio.gather(first, second)
    finally:
        chat_session.current_session.reset(session_context)

    assert claims.count("opaque-ticket") == 1
    assert claims.count(None) == 1


async def test_direct_tts_without_chat_session_cannot_steal_handoff() -> None:
    """A direct TTS call stays cold and leaves the Assist ticket claimable."""
    synthesize = AsyncMock(
        return_value=BridgeAudio(data=b"\x00\x01", audio_format="pcm")
    )
    client = SimpleNamespace(
        async_synthesize=synthesize,
        async_release_speech_session_handoff=AsyncMock(),
    )
    entity = _make_tts_entity(client)
    session = chat_session.ChatSession("assist-session")
    session_context = chat_session.current_session.set(session)
    try:
        pipeline_options = _offer_handoff(client)
        direct_context = chat_session.current_session.set(None)
        try:
            await entity.async_get_tts_audio("Direct", "en-US", {})
        finally:
            chat_session.current_session.reset(direct_context)
        await entity.async_get_tts_audio("Assist", "en-US", pipeline_options)
    finally:
        chat_session.current_session.reset(session_context)

    assert "speech_session_handoff_token" not in synthesize.await_args_list[0].kwargs
    assert (
        synthesize.await_args_list[1].kwargs["speech_session_handoff_token"]
        == "opaque-ticket"
    )


async def test_direct_tts_in_same_session_after_stt_cannot_steal_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only HA's saved pre-STT options can claim after transcription finishes."""
    monkeypatch.setattr(
        tts_module,
        "_AUTOMATIC_SPEECH_SESSION_HANDOFF_ENABLED",
        True,
    )
    synthesize = AsyncMock(
        return_value=BridgeAudio(data=b"\x00\x01", audio_format="pcm")
    )
    client = SimpleNamespace(
        async_synthesize=synthesize,
        async_release_speech_session_handoff=AsyncMock(),
    )
    entity = _make_tts_entity(client)
    session_context = chat_session.current_session.set(
        chat_session.ChatSession("assist-session")
    )
    try:
        pipeline_options = dict(entity.default_options)
        _store_prepared_handoff(client)
        direct_options = dict(entity.default_options)
        assert api_module._SPEECH_SESSION_HANDOFF_OPTION not in direct_options

        await entity.async_get_tts_audio("Direct", "en-US", direct_options)
        await entity.async_get_tts_audio("Assist", "en-US", pipeline_options)
    finally:
        chat_session.current_session.reset(session_context)

    assert "speech_session_handoff_token" not in synthesize.await_args_list[0].kwargs
    assert synthesize.await_args_list[1].kwargs["speech_session_handoff_token"] == (
        "opaque-ticket"
    )


async def test_handoff_language_normalization_matches_equivalent_tag() -> None:
    """Equivalent underscore, case, and whitespace variants keep the warm path."""
    synthesize = AsyncMock(
        return_value=BridgeAudio(data=b"\x00\x01", audio_format="pcm")
    )
    client = SimpleNamespace(
        async_synthesize=synthesize,
        async_release_speech_session_handoff=AsyncMock(),
    )
    entity = _make_tts_entity(client)
    session_context = chat_session.current_session.set(
        chat_session.ChatSession("assist-session")
    )
    try:
        pipeline_options = _offer_handoff(client, language=" EN_us ")
        await entity.async_get_tts_audio("Assist", "en-US", pipeline_options)
    finally:
        chat_session.current_session.reset(session_context)

    synthesize.assert_awaited_once_with(
        "Assist",
        language="en-US",
        voice="cove",
        instructions=None,
        speech_session_handoff_token="opaque-ticket",
    )


@pytest.mark.parametrize("language", ["en-GB", "es-US"])
async def test_handoff_language_mismatch_stays_cold_and_releases(
    language: str,
) -> None:
    """A different region or primary language cannot reuse the STT session."""
    synthesize = AsyncMock(
        return_value=BridgeAudio(data=b"\x00\x01", audio_format="pcm")
    )
    release = AsyncMock()
    client = SimpleNamespace(
        async_synthesize=synthesize,
        async_release_speech_session_handoff=release,
    )
    entity = _make_tts_entity(client)
    session_context = chat_session.current_session.set(
        chat_session.ChatSession("assist-session")
    )
    try:
        pipeline_options = _offer_handoff(client, language="en-US")
        await entity.async_get_tts_audio("Assist", language, pipeline_options)
        await _wait_for_handoff_releases()
    finally:
        chat_session.current_session.reset(session_context)

    assert synthesize.await_args is not None
    assert "speech_session_handoff_token" not in synthesize.await_args.kwargs
    release.assert_awaited_once_with("opaque-ticket")


async def test_expired_handoff_stays_cold_and_is_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local monotonic expiry revokes the ticket before finite TTS awaits."""
    synthesize = AsyncMock(
        return_value=BridgeAudio(data=b"\x00\x01", audio_format="pcm")
    )
    release = AsyncMock()
    client = SimpleNamespace(
        async_synthesize=synthesize,
        async_release_speech_session_handoff=release,
    )
    entity = _make_tts_entity(client)
    session_context = chat_session.current_session.set(
        chat_session.ChatSession("assist-session")
    )
    try:
        pipeline_options = _offer_handoff(client)
        pending = api_module._PENDING_SPEECH_SESSION_HANDOFF.get()
        assert pending is not None
        monkeypatch.setattr(
            api_module,
            "monotonic",
            lambda: pending.expires_at + 1,
        )
        await entity.async_get_tts_audio("Expired", "en-US", pipeline_options)
        await _wait_for_handoff_releases()
    finally:
        chat_session.current_session.reset(session_context)

    assert synthesize.await_args is not None
    assert "speech_session_handoff_token" not in synthesize.await_args.kwargs
    release.assert_awaited_once_with("opaque-ticket")


async def test_cancelled_finite_tts_releases_claimed_handoff() -> None:
    """Cancellation propagates after privately releasing a claimed ticket."""
    synthesis_started = asyncio.Event()

    async def synthesize(*args: Any, **kwargs: Any) -> BridgeAudio:
        synthesis_started.set()
        await asyncio.Event().wait()
        raise AssertionError

    release = AsyncMock()
    client = SimpleNamespace(
        async_synthesize=synthesize,
        async_release_speech_session_handoff=release,
    )
    entity = _make_tts_entity(client)
    session_context = chat_session.current_session.set(
        chat_session.ChatSession("assist-session")
    )
    try:
        pipeline_options = _offer_handoff(client)
        task = asyncio.create_task(
            entity.async_get_tts_audio("Cancelled", "en-US", pipeline_options)
        )
        await asyncio.wait_for(synthesis_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _wait_for_handoff_releases()
    finally:
        chat_session.current_session.reset(session_context)

    release.assert_awaited_once_with("opaque-ticket")


async def test_client_and_session_identity_mismatches_do_not_claim() -> None:
    """Only the exact client and ChatSession identities can claim a ticket."""
    first_client = SimpleNamespace(async_release_speech_session_handoff=AsyncMock())
    second_client = SimpleNamespace(async_release_speech_session_handoff=AsyncMock())
    first_session = chat_session.ChatSession("same-id")
    session_context = chat_session.current_session.set(first_session)
    try:
        pipeline_options = _offer_handoff(first_client)
        assert (
            api_module._claim_speech_session_handoff(
                cast("BridgeClient", second_client),
                language="en-US",
                voice="cove",
                instructions=None,
                options=dict(pipeline_options),
            )
            is None
        )
        other_session_context = chat_session.current_session.set(
            chat_session.ChatSession("same-id")
        )
        try:
            assert (
                api_module._claim_speech_session_handoff(
                    cast("BridgeClient", first_client),
                    language="en-US",
                    voice="cove",
                    instructions=None,
                    options=dict(pipeline_options),
                )
                is None
            )
        finally:
            chat_session.current_session.reset(other_session_context)
        claimed = api_module._claim_speech_session_handoff(
            cast("BridgeClient", first_client),
            language="en-US",
            voice="cove",
            instructions=None,
            options=dict(pipeline_options),
        )
    finally:
        chat_session.current_session.reset(session_context)

    assert claimed == "opaque-ticket"


@pytest.mark.parametrize(
    ("voice", "instructions"),
    [("ember", None), ("cove", "Unverified instructions")],
)
async def test_profile_mismatch_revokes_handoff(
    voice: str,
    instructions: str | None,
) -> None:
    """Incompatible synthesis profiles stay cold and release the warm offer."""
    release = AsyncMock()
    client = SimpleNamespace(async_release_speech_session_handoff=release)
    session_context = chat_session.current_session.set(
        chat_session.ChatSession("assist-session")
    )
    try:
        pipeline_options = _offer_handoff(client)
        claimed = api_module._claim_speech_session_handoff(
            cast("BridgeClient", client),
            language="en-US",
            voice=voice,
            instructions=instructions,
            options=dict(pipeline_options),
        )
        await _wait_for_handoff_releases()
        replay = api_module._claim_speech_session_handoff(
            cast("BridgeClient", client),
            language="en-US",
            voice="cove",
            instructions=None,
            options=dict(pipeline_options),
        )
    finally:
        chat_session.current_session.reset(session_context)

    assert claimed is None
    assert replay is None
    release.assert_awaited_once_with("opaque-ticket")


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
                ATTR_PREFERRED_FORMAT: "wav",
                ATTR_PREFERRED_SAMPLE_RATE: 16000,
                ATTR_PREFERRED_SAMPLE_CHANNELS: 1,
                ATTR_PREFERRED_SAMPLE_BYTES: 2,
            },
            message_gen=_message_chunks("Hel", "lo"),
        )
    )

    assert entity.async_supports_streaming_input()
    assert entity.supported_options is not None
    assert {
        ATTR_PREFERRED_FORMAT,
        ATTR_PREFERRED_SAMPLE_RATE,
        ATTR_PREFERRED_SAMPLE_CHANNELS,
        ATTR_PREFERRED_SAMPLE_BYTES,
    }.issubset(entity.supported_options)
    assert api_module._SPEECH_SESSION_HANDOFF_OPTION not in entity.supported_options
    assert api_module._SPEECH_SESSION_HANDOFF_OPTION not in entity.default_options
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
        sample_rate=16000,
        channels=1,
        sample_width=2,
    )


async def test_tts_stream_claims_handoff_before_advancing_message() -> None:
    """Streaming TTS claims synchronously before its first message-generator await."""
    client: Any

    async def message_stream() -> AsyncGenerator[str]:
        assert (
            api_module._claim_speech_session_handoff(
                cast("BridgeClient", client),
                language="en-US",
                voice="cove",
                instructions=None,
                options={
                    api_module._SPEECH_SESSION_HANDOFF_OPTION: (
                        api_module._SPEECH_SESSION_HANDOFF_OPTION_VALUE
                    )
                },
            )
            is None
        )
        yield "Hello"

    async def audio_stream() -> AsyncGenerator[bytes]:
        yield b"RIFF-stream-header"

    synthesize_stream = Mock(return_value=audio_stream())
    client = SimpleNamespace(
        async_synthesize_stream=synthesize_stream,
        async_release_speech_session_handoff=AsyncMock(),
    )
    entity = _make_tts_entity(client)
    session_context = chat_session.current_session.set(
        chat_session.ChatSession("assist-session")
    )
    try:
        pipeline_options = _offer_handoff(client)
        response = await entity.async_stream_tts_audio(
            TTSAudioRequest("en-US", pipeline_options, message_stream())
        )
        assert [chunk async for chunk in response.data_gen] == [b"RIFF-stream-header"]
    finally:
        chat_session.current_session.reset(session_context)

    synthesize_stream.assert_called_once_with(
        "Hello",
        language="en-US",
        voice="cove",
        instructions=None,
        speech_session_handoff_token="opaque-ticket",
    )


async def test_tts_stream_releases_handoff_when_inner_close_is_cancelled() -> None:
    """A cancelled HTTP-generator close cannot strand the claimed bridge offer."""

    async def audio_stream() -> AsyncGenerator[bytes]:
        try:
            yield b"RIFF-stream-header"
        finally:
            raise asyncio.CancelledError

    release = AsyncMock()
    client = SimpleNamespace(
        async_synthesize_stream=Mock(return_value=audio_stream()),
        async_release_speech_session_handoff=release,
    )
    entity = _make_tts_entity(client)
    session_context = chat_session.current_session.set(
        chat_session.ChatSession("assist-session")
    )
    try:
        pipeline_options = _offer_handoff(client)
        response = await entity.async_stream_tts_audio(
            TTSAudioRequest("en-US", pipeline_options, _message_chunks("Hello"))
        )
        assert await anext(response.data_gen) == b"RIFF-stream-header"
        with pytest.raises(asyncio.CancelledError):
            await response.data_gen.aclose()
        await _wait_for_handoff_releases()
    finally:
        chat_session.current_session.reset(session_context)

    release.assert_awaited_once_with("opaque-ticket")


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
    cast("Mock", entry.async_start_reauth).assert_called_once_with(entity.hass)


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
