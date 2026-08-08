"""Tests for the Codex Voice bridge client."""

from __future__ import annotations

import asyncio
import base64
import io
import wave
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
from aiohttp import web

from custom_components.codex_voice import api as api_module
from custom_components.codex_voice.api import (
    BridgeAuthenticationError,
    BridgeBusyError,
    BridgeClient,
    BridgeConnectionError,
    BridgeProtocolError,
    BridgeQuotaError,
    BridgeToolCall,
)


def _wav_audio(pcm: bytes = b"\x00\x01\x02\x03") -> bytes:
    """Return a small, valid PCM16 WAV file."""
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24000)
        wav_file.writeframes(pcm)
    return output.getvalue()


def _streaming_wav_audio(pcm: bytes = b"\x00\x01\x02\x03") -> bytes:
    """Return the bridge's canonical EOF-terminated PCM16 WAV framing."""
    audio = bytearray(_wav_audio(pcm))
    audio[4:8] = b"\xff\xff\xff\xff"
    audio[40:44] = b"\xff\xff\xff\xff"
    return bytes(audio)


async def test_health_and_authentication(
    aiohttp_client: Any,
    socket_enabled: None,
) -> None:
    """Health requests authenticate and reject an invalid token."""

    async def health(request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != "Bearer good-token":
            raise web.HTTPUnauthorized
        return web.json_response({"status": "ok", "version": "1.0"})

    app = web.Application()
    app.router.add_get("/health", health)
    test_client = await aiohttp_client(app)

    client = BridgeClient(
        test_client.session, str(test_client.make_url("")), "good-token"
    )
    assert await client.async_health() == {"status": "ok", "version": "1.0"}

    invalid_client = BridgeClient(
        test_client.session,
        str(test_client.make_url("")),
        "bad-token",
    )
    with pytest.raises(BridgeAuthenticationError):
        await invalid_client.async_health()


async def test_conversation_stream_and_tool_result(
    aiohttp_client: Any,
    socket_enabled: None,
) -> None:
    """Conversation deltas and Home Assistant tool results are bidirectional."""
    received: dict[str, Any] = {}

    async def conversation(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        received["start"] = await websocket.receive_json()
        await websocket.send_json(
            {"type": "started", "conversation_id": "conversation-1"}
        )
        await websocket.send_json(
            {"type": "item", "event": "started", "item": {"type": "message"}}
        )
        await websocket.send_json(
            {"type": "event", "method": "turn/started", "params": {}}
        )
        await websocket.send_json({"type": "delta", "delta": "Lights "})
        await websocket.send_json(
            {
                "type": "tool_call",
                "request_id": 42,
                "call_id": "call-1",
                "name": "HassTurnOn",
                "arguments": {"name": "Kitchen"},
            }
        )
        received["tool_result"] = await websocket.receive_json()
        await websocket.send_json({"type": "delta", "content": "are on."})
        await websocket.send_json({"type": "done"})
        await websocket.close()
        return websocket

    app = web.Application()
    app.router.add_get("/v1/conversation", conversation)
    test_client = await aiohttp_client(app)
    client = BridgeClient(test_client.session, str(test_client.make_url("")), "token")

    deltas: list[str] = []
    tool_calls: list[BridgeToolCall] = []

    async def handle_delta(delta: str) -> None:
        deltas.append(delta)

    async def handle_tool(tool_call: BridgeToolCall) -> dict[str, Any]:
        tool_calls.append(tool_call)
        return {"success": True}

    done = await client.async_converse(
        {"conversation_id": "conversation-1", "messages": []},
        async_handle_delta=handle_delta,
        async_handle_tool=handle_tool,
    )

    assert done == {"type": "done"}
    assert deltas == ["Lights ", "are on."]
    assert tool_calls == [
        BridgeToolCall(
            call_id="call-1",
            name="HassTurnOn",
            arguments={"name": "Kitchen"},
            request_id=42,
        )
    ]
    assert received["start"]["type"] == "start"
    assert received["tool_result"] == {
        "type": "tool_result",
        "request_id": 42,
        "call_id": "call-1",
        "result": {"success": True},
        "success": True,
    }


async def test_transcribe_sends_base64_pcm(
    aiohttp_client: Any,
    socket_enabled: None,
) -> None:
    """Transcription sends bounded base64 PCM and metadata."""
    received: dict[str, Any] = {}

    async def transcribe(request: web.Request) -> web.Response:
        received.update(await request.json())
        return web.json_response({"text": "Turn on the kitchen"})

    app = web.Application()
    app.router.add_post("/v1/transcribe", transcribe)
    test_client = await aiohttp_client(app)
    client = BridgeClient(test_client.session, str(test_client.make_url("")), "token")

    result = await client.async_transcribe(
        b"\x01\x02",
        {"sample_rate": 16000, "channels": 1, "language": "en-US"},
        prompt=None,
    )

    assert result == "Turn on the kitchen"
    assert base64.b64decode(received["audio"]) == b"\x01\x02"
    assert received["format"] == "pcm"
    assert received["sample_rate"] == 16000


async def test_synthesize_decodes_json_pcm(
    aiohttp_client: Any,
    socket_enabled: None,
) -> None:
    """Synthesis accepts the bridge's base64 PCM response."""

    async def synthesize(request: web.Request) -> web.Response:
        payload = await request.json()
        assert payload["voice"] == "alloy"
        return web.json_response(
            {
                "audio": base64.b64encode(b"\x00\x01").decode(),
                "format": "pcm",
                "sample_rate": 24000,
                "channels": 1,
                "sample_width": 2,
            }
        )

    app = web.Application()
    app.router.add_post("/v1/synthesize", synthesize)
    test_client = await aiohttp_client(app)
    client = BridgeClient(test_client.session, str(test_client.make_url("")), "token")

    audio = await client.async_synthesize(
        "Hello",
        language="en-US",
        voice="alloy",
        instructions=None,
    )

    assert audio.data == b"\x00\x01"
    assert audio.audio_format == "pcm"
    assert audio.sample_rate == 24000


async def test_synthesize_rejects_mislabeled_wav(
    aiohttp_client: Any,
    socket_enabled: None,
) -> None:
    """Binary synthesis responses must contain a recognizable WAV file."""

    async def synthesize(request: web.Request) -> web.Response:
        return web.Response(body=b"not a wave file", content_type="audio/wav")

    app = web.Application()
    app.router.add_post("/v1/synthesize", synthesize)
    test_client = await aiohttp_client(app)
    client = BridgeClient(test_client.session, str(test_client.make_url("")), "token")

    with pytest.raises(BridgeProtocolError, match="valid WAV"):
        await client.async_synthesize(
            "Hello",
            language="en-US",
            voice="alloy",
            instructions=None,
        )


async def test_synthesize_stream_yields_before_response_completes(
    aiohttp_client: Any,
    socket_enabled: None,
) -> None:
    """Streaming synthesis validates its header and applies backpressure."""
    audio = _streaming_wav_audio()
    header_started = asyncio.Event()
    release_header = asyncio.Event()
    release_audio = asyncio.Event()
    received: dict[str, Any] = {}

    async def synthesize(request: web.Request) -> web.StreamResponse:
        assert request.headers["Authorization"] == "Bearer token"
        received.update(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "audio/wav"})
        await response.prepare(request)
        await response.write(audio[:5])
        header_started.set()
        await release_header.wait()
        await response.write(audio[5:44])
        await release_audio.wait()
        await response.write(audio[44:])
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/synthesize/stream", synthesize)
    test_client = await aiohttp_client(app)
    client = BridgeClient(test_client.session, str(test_client.make_url("")), "token")
    stream = client.async_synthesize_stream(
        "Hello",
        language="en-US",
        voice="cove",
        instructions="Be brief",
    )

    first_chunk_task = asyncio.create_task(anext(stream))
    await asyncio.wait_for(header_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not first_chunk_task.done()

    release_header.set()
    try:
        await asyncio.sleep(0)
        assert not first_chunk_task.done()
        release_audio.set()
        first_chunk = await asyncio.wait_for(first_chunk_task, timeout=1)
        assert len(first_chunk) >= 46
        assert first_chunk[:4] == b"RIFF"
        assert first_chunk[8:12] == b"WAVE"
        remaining = [chunk async for chunk in stream]
    finally:
        release_header.set()
        release_audio.set()
        await stream.aclose()

    assert b"".join([first_chunk, *remaining]) == audio
    assert received == {
        "text": "Hello",
        "language": "en-US",
        "voice": "cove",
        "format": "wav",
        "instructions": "Be brief",
    }


async def test_synthesize_stream_rejects_fragmented_invalid_header(
    aiohttp_client: Any,
    socket_enabled: None,
) -> None:
    """No bytes are exposed before a fragmented WAV header is validated."""

    async def synthesize(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "audio/wav"})
        await response.prepare(request)
        await response.write(b"not ")
        await response.write(b"a wav header")
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/synthesize/stream", synthesize)
    test_client = await aiohttp_client(app)
    client = BridgeClient(test_client.session, str(test_client.make_url("")), "token")

    with pytest.raises(BridgeProtocolError, match="valid WAV"):
        _ = [
            chunk
            async for chunk in client.async_synthesize_stream(
                "Hello",
                language="en-US",
                voice="cove",
                instructions=None,
            )
        ]


async def test_synthesize_stream_enforces_size_limit(
    aiohttp_client: Any,
    socket_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunked synthesis cannot exceed the client-side audio size limit."""
    monkeypatch.setattr(api_module, "MAX_SYNTHESIZED_AUDIO_BYTES", 16)
    audio = _streaming_wav_audio()

    async def synthesize(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "audio/wav"})
        await response.prepare(request)
        await response.write(audio[:12])
        await response.write(audio[12:17])
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/synthesize/stream", synthesize)
    test_client = await aiohttp_client(app)
    client = BridgeClient(test_client.session, str(test_client.make_url("")), "token")

    with pytest.raises(BridgeProtocolError, match="50 MiB"):
        _ = [
            chunk
            async for chunk in client.async_synthesize_stream(
                "Hello",
                language="en-US",
                voice="cove",
                instructions=None,
            )
        ]


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, BridgeAuthenticationError),
        (409, BridgeBusyError),
        (429, BridgeQuotaError),
        (500, BridgeConnectionError),
        (400, BridgeProtocolError),
    ],
)
async def test_synthesize_stream_maps_http_errors(
    aiohttp_client: Any,
    socket_enabled: None,
    status: int,
    error_type: type[Exception],
) -> None:
    """Streaming synthesis preserves the bridge's stable HTTP error mapping."""

    async def synthesize(request: web.Request) -> web.Response:
        return web.Response(status=status)

    app = web.Application()
    app.router.add_post("/v1/synthesize/stream", synthesize)
    test_client = await aiohttp_client(app)
    client = BridgeClient(test_client.session, str(test_client.make_url("")), "token")

    with pytest.raises(error_type):
        _ = [
            chunk
            async for chunk in client.async_synthesize_stream(
                "Hello",
                language="en-US",
                voice="cove",
                instructions=None,
            )
        ]


async def test_synthesize_stream_closes_response_when_consumer_stops() -> None:
    """Closing a partially consumed generator promptly closes its HTTP response."""
    audio = _streaming_wav_audio()

    class StreamContent:
        async def iter_chunked(self, chunk_size: int) -> AsyncGenerator[bytes]:
            yield audio[:46]
            await asyncio.Event().wait()

    class StreamResponse:
        status = 200
        content_type = "audio/wav"
        content_length = None
        content = StreamContent()
        closed = False

        def close(self) -> None:
            self.closed = True

    response = StreamResponse()

    class RequestContext:
        async def __aenter__(self) -> StreamResponse:
            return response

        async def __aexit__(self, *args: Any) -> None:
            response.close()

    class Session:
        def post(self, *args: Any, **kwargs: Any) -> RequestContext:
            return RequestContext()

    client = BridgeClient(cast("Any", Session()), "http://bridge", "token")
    stream = client.async_synthesize_stream(
        "Hello",
        language="en-US",
        voice="cove",
        instructions=None,
    )

    _ = await asyncio.wait_for(anext(stream), timeout=1)
    await stream.aclose()

    assert response.closed
