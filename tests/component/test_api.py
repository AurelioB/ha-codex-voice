"""Tests for the Codex Voice bridge client."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from aiohttp import web

from custom_components.codex_voice.api import (
    BridgeAuthenticationError,
    BridgeClient,
    BridgeProtocolError,
    BridgeToolCall,
)


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
