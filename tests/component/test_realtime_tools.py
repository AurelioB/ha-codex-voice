"""Tests for the outbound realtime Home Assistant tool broker."""

from __future__ import annotations

import asyncio
from typing import Any, override

import pytest
import voluptuous as vol
from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.json import json_dumps_sorted
from homeassistant.util.json import JsonObjectType, json_loads_object
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.codex_voice.const import (
    CONF_ACCESS_TOKEN,
    CONF_BRIDGE_URL,
    CONF_REALTIME_AUTHORITY,
    CONF_REALTIME_LANGUAGE,
    DOMAIN,
    MAX_REALTIME_TOOL_ARGUMENT_BYTES,
    MAX_REALTIME_TOOL_RESULT_BYTES,
)
from custom_components.codex_voice.realtime_tools import (
    RealtimeToolBroker,
    RealtimeToolBrokerError,
    select_realtime_authority,
)


class RecordingTool(llm.Tool):
    """A deterministic test tool that records every execution."""

    name = "HassTurnOn"
    description = "Turn on one exposed Home Assistant entity"
    parameters = vol.Schema({vol.Required("name"): str})

    def __init__(self) -> None:
        """Initialize the recording tool."""
        self.calls: list[llm.ToolInput] = []

    @override
    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Record and return a bounded result unless asked for a large one."""
        self.calls.append(tool_input)
        if tool_input.tool_args["name"] == "raises":
            raise RuntimeError("private test failure")
        if tool_input.tool_args["name"] == "hangs":
            await asyncio.Event().wait()
        if tool_input.tool_args["name"] == "oversized-result":
            return {"data": "x" * (MAX_REALTIME_TOOL_RESULT_BYTES + 1)}
        return {"changed": True, "name": tool_input.tool_args["name"]}


class RecordingAPI(llm.API):
    """A test LLM API that records the broker's context."""

    def __init__(self, hass: HomeAssistant, tool: RecordingTool) -> None:
        """Initialize the test API."""
        super().__init__(hass=hass, id="test-assist", name="Test Assist")
        self.tool = tool
        self.contexts: list[llm.LLMContext] = []

    @override
    async def async_get_api_instance(
        self,
        llm_context: llm.LLMContext,
    ) -> llm.APIInstance:
        """Return one captured API instance."""
        self.contexts.append(llm_context)
        return llm.APIInstance(
            api=self,
            api_prompt="Only operate entities exposed by Home Assistant.",
            llm_context=llm_context,
            tools=[self.tool],
        )


class ConcurrentTool(RecordingTool):
    """Tool that proves calls from one generation can overlap safely."""

    def __init__(self) -> None:
        """Initialize concurrency coordination."""
        super().__init__()
        self.two_active = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0

    @override
    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Wait until two calls overlap, then return both results."""
        self.calls.append(tool_input)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active == 2:
            self.two_active.set()
        await self.release.wait()
        self.active -= 1
        return {"name": tool_input.tool_args["name"]}


def _entry(
    bridge_url: str,
    *,
    authorities: tuple[bool, ...] = (True,),
    language: str = "es-MX",
    prompt: str = "You are the assistant for {{ ha_name }}.",
) -> MockConfigEntry:
    """Create an entry with the requested Conversation authority layout."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_BRIDGE_URL: bridge_url, CONF_ACCESS_TOKEN: "secret-token"},
        minor_version=2,
        subentries_data=[
            {
                "data": {
                    "llm_hass_api": ["test-assist"],
                    "prompt": prompt,
                    CONF_REALTIME_AUTHORITY: authority,
                    CONF_REALTIME_LANGUAGE: language,
                },
                "subentry_type": "conversation",
                "title": f"Conversation {index}",
                "unique_id": None,
            }
            for index, authority in enumerate(authorities)
        ],
    )


async def test_authenticated_registration_and_generation_scoped_calls(
    hass: HomeAssistant,
    aiohttp_client: Any,
    socket_enabled: None,
) -> None:
    """Only live advertised calls execute and every result is canonical once."""
    tool = RecordingTool()
    api = RecordingAPI(hass, tool)
    unregister = llm.async_register_api(hass, api)
    received: dict[str, Any] = {}

    async def tools(request: web.Request) -> web.WebSocketResponse:
        received["authorization"] = request.headers.get("Authorization")
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        registration = await websocket.receive()
        received["registration_raw"] = registration.data
        received["registration"] = json_loads_object(registration.data)
        await websocket.send_json(
            {"type": "registered", "protocol_version": 1, "generation": "g-1"}
        )
        # A stale generation is ignored and cannot execute a Home Assistant tool.
        await websocket.send_json(
            {
                "type": "tool_call",
                "generation": "old-generation",
                "call_id": "stale",
                "name": "HassTurnOn",
                "arguments": {"name": "Bedroom"},
            }
        )
        await websocket.send_json(
            {
                "type": "tool_call",
                "generation": "g-1",
                "call_id": "call-1",
                "name": "HassTurnOn",
                "arguments": {"name": "Kitchen"},
            }
        )
        first = await websocket.receive()
        received["first_raw"] = first.data
        received["first"] = json_loads_object(first.data)
        # A duplicate is ignored. The following unknown tool receives the next
        # (and only) result event without executing anything.
        await websocket.send_json(
            {
                "type": "tool_call",
                "generation": "g-1",
                "call_id": "call-1",
                "name": "HassTurnOn",
                "arguments": {"name": "Duplicate"},
            }
        )
        await websocket.send_json(
            {
                "type": "tool_call",
                "generation": "g-1",
                "call_id": "call-2",
                "name": "NotAdvertised",
                "arguments": {},
            }
        )
        second = await websocket.receive()
        received["second"] = json_loads_object(second.data)
        await websocket.send_json({"type": "ping"})
        received["pong"] = await websocket.receive_json()
        await websocket.close()
        return websocket

    app = web.Application()
    app.router.add_get("/v1/home-assistant/tools", tools)
    test_client = await aiohttp_client(app)
    entry = _entry(str(test_client.make_url("")))
    authority = select_realtime_authority(entry)
    assert authority is not None
    broker = RealtimeToolBroker(hass, entry, authority, test_client.session)

    snapshot = await broker._async_prepare_registration()
    async with asyncio.timeout(3):
        assert await broker._async_connect(snapshot)
    unregister()

    registration = received["registration"]
    assert received["authorization"] == "Bearer secret-token"
    assert received["registration_raw"] == json_dumps_sorted(registration)
    assert registration["type"] == "register"
    assert registration["protocol_version"] == 1
    assert registration["authority_id"] == authority.subentry_id
    assert registration["language"] == "es-MX"
    assert "assistant for test home" in registration["instructions"]
    assert "Only operate entities exposed" in registration["instructions"]
    assert "Respond using language and locale es-MX" in registration["instructions"]
    assert [item["name"] for item in registration["tools"]] == ["HassTurnOn"]
    assert api.contexts[0].platform == DOMAIN
    assert api.contexts[0].assistant == DOMAIN
    assert api.contexts[0].language == "es-MX"
    assert api.contexts[0].context is None

    assert received["first_raw"] == json_dumps_sorted(received["first"])
    assert received["first"] == {
        "type": "tool_result",
        "generation": "g-1",
        "call_id": "call-1",
        "success": True,
        "result": {"changed": True, "name": "Kitchen"},
    }
    assert received["second"]["call_id"] == "call-2"
    assert received["second"]["success"] is False
    assert received["second"]["result"]["error"] == "tool_not_available"
    assert received["pong"] == {"type": "pong"}
    assert [call.id for call in tool.calls] == ["call-1"]


async def test_invalid_arguments_and_oversized_results_fail_closed(
    hass: HomeAssistant,
    aiohttp_client: Any,
    socket_enabled: None,
) -> None:
    """Bad arguments never execute and large results are replaced safely."""
    tool = RecordingTool()
    unregister = llm.async_register_api(hass, RecordingAPI(hass, tool))
    received: list[dict[str, Any]] = []

    async def tools(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        await websocket.receive()
        await websocket.send_json(
            {"type": "registered", "protocol_version": 1, "generation": "g-2"}
        )
        await websocket.send_json(
            {
                "type": "tool_call",
                "generation": "g-2",
                "call_id": "bad-shape",
                "name": "HassTurnOn",
                "arguments": ["not", "an", "object"],
            }
        )
        received.append(await websocket.receive_json())
        await websocket.send_json(
            {
                "type": "tool_call",
                "generation": "g-2",
                "call_id": "too-large",
                "name": "HassTurnOn",
                "arguments": {"name": "x" * (MAX_REALTIME_TOOL_ARGUMENT_BYTES + 1)},
            }
        )
        received.append(await websocket.receive_json())
        await websocket.send_json(
            {
                "type": "tool_call",
                "generation": "g-2",
                "call_id": "large-result",
                "name": "HassTurnOn",
                "arguments": {"name": "oversized-result"},
            }
        )
        received.append(await websocket.receive_json())
        await websocket.send_json(
            {
                "type": "tool_call",
                "generation": "g-2",
                "call_id": "tool-exception",
                "name": "HassTurnOn",
                "arguments": {"name": "raises"},
            }
        )
        received.append(await websocket.receive_json())
        await websocket.close()
        return websocket

    app = web.Application()
    app.router.add_get("/v1/home-assistant/tools", tools)
    test_client = await aiohttp_client(app)
    entry = _entry(str(test_client.make_url("")))
    authority = select_realtime_authority(entry)
    assert authority is not None
    broker = RealtimeToolBroker(hass, entry, authority, test_client.session)
    assert await broker._async_connect(await broker._async_prepare_registration())
    unregister()

    assert [item["call_id"] for item in received] == [
        "bad-shape",
        "too-large",
        "large-result",
        "tool-exception",
    ]
    assert all(item["success"] is False for item in received)
    assert received[0]["result"]["error"] == "invalid_arguments"
    assert received[1]["result"]["error"] == "invalid_arguments"
    assert received[2]["result"]["error"] == "tool_failed"
    assert received[3]["result"]["error"] == "tool_failed"
    assert [call.id for call in tool.calls] == ["large-result", "tool-exception"]


async def test_tool_calls_execute_concurrently_with_serialized_results(
    hass: HomeAssistant,
    aiohttp_client: Any,
    socket_enabled: None,
) -> None:
    """One slow HA action does not block another realtime tool request."""
    tool = ConcurrentTool()
    unregister = llm.async_register_api(hass, RecordingAPI(hass, tool))
    received: list[dict[str, Any]] = []

    async def tools(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        await websocket.receive()
        await websocket.send_json(
            {"type": "registered", "protocol_version": 1, "generation": "g-3"}
        )
        for call_id, name in (("first", "Kitchen"), ("second", "Bedroom")):
            await websocket.send_json(
                {
                    "type": "tool_call",
                    "generation": "g-3",
                    "call_id": call_id,
                    "name": "HassTurnOn",
                    "arguments": {"name": name},
                }
            )
        async with asyncio.timeout(1):
            await tool.two_active.wait()
        tool.release.set()
        received.extend(
            [await websocket.receive_json(), await websocket.receive_json()]
        )
        await websocket.close()
        return websocket

    app = web.Application()
    app.router.add_get("/v1/home-assistant/tools", tools)
    test_client = await aiohttp_client(app)
    entry = _entry(str(test_client.make_url("")))
    authority = select_realtime_authority(entry)
    assert authority is not None
    broker = RealtimeToolBroker(hass, entry, authority, test_client.session)

    assert await broker._async_connect(await broker._async_prepare_registration())
    unregister()

    assert tool.max_active == 2
    assert {item["call_id"] for item in received} == {"first", "second"}
    assert all(item["success"] is True for item in received)


async def test_tool_call_timeout_returns_stable_failure(
    hass: HomeAssistant,
    aiohttp_client: Any,
    socket_enabled: None,
) -> None:
    """A stuck HA tool ends before the bridge correlation deadline."""
    tool = RecordingTool()
    unregister = llm.async_register_api(hass, RecordingAPI(hass, tool))
    received: dict[str, Any] = {}

    async def tools(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        await websocket.receive()
        await websocket.send_json(
            {"type": "registered", "protocol_version": 1, "generation": "g-4"}
        )
        await websocket.send_json(
            {
                "type": "tool_call",
                "generation": "g-4",
                "call_id": "stuck-call",
                "name": "HassTurnOn",
                "arguments": {"name": "hangs"},
            }
        )
        received.update(await websocket.receive_json())
        await websocket.close()
        return websocket

    app = web.Application()
    app.router.add_get("/v1/home-assistant/tools", tools)
    test_client = await aiohttp_client(app)
    entry = _entry(str(test_client.make_url("")))
    authority = select_realtime_authority(entry)
    assert authority is not None
    broker = RealtimeToolBroker(
        hass,
        entry,
        authority,
        test_client.session,
        tool_call_timeout=0.01,
    )

    assert await broker._async_connect(await broker._async_prepare_registration())
    unregister()

    assert received["call_id"] == "stuck-call"
    assert received["success"] is False
    assert received["result"]["error"] == "tool_timeout"
    assert [call.id for call in tool.calls] == ["stuck-call"]


def test_authority_selection_fails_closed_for_zero_or_multiple(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The broker never guesses when authority configuration is ambiguous."""
    assert (
        select_realtime_authority(_entry("http://bridge", authorities=(False,))) is None
    )
    assert (
        select_realtime_authority(_entry("http://bridge", authorities=(True, True)))
        is None
    )
    assert len(caplog.messages) == 2
    assert "found 0" in caplog.messages[0]
    assert "found 2" in caplog.messages[1]


async def test_registration_rejects_unsupported_language_and_large_prompt(
    hass: HomeAssistant,
    aiohttp_client: Any,
    socket_enabled: None,
) -> None:
    """Invalid authority metadata never reaches the bridge."""
    unregister = llm.async_register_api(hass, RecordingAPI(hass, RecordingTool()))
    app = web.Application()
    test_client = await aiohttp_client(app)

    entry = _entry(str(test_client.make_url("")), language="xx-INVALID")
    authority = select_realtime_authority(entry)
    assert authority is not None
    broker = RealtimeToolBroker(hass, entry, authority, test_client.session)
    with pytest.raises(RealtimeToolBrokerError, match="Unsupported"):
        await broker._async_prepare_registration()

    entry = _entry(str(test_client.make_url("")), prompt="x" * (65 * 1024))
    authority = select_realtime_authority(entry)
    assert authority is not None
    broker = RealtimeToolBroker(hass, entry, authority, test_client.session)
    with pytest.raises(RealtimeToolBrokerError, match="instructions"):
        await broker._async_prepare_registration()
    unregister()


async def test_reconnects_with_fresh_generation_and_stops_cleanly(
    hass: HomeAssistant,
    aiohttp_client: Any,
    socket_enabled: None,
) -> None:
    """Disconnects create fresh snapshots and unload cancels the background task."""
    api = RecordingAPI(hass, RecordingTool())
    unregister = llm.async_register_api(hass, api)
    connections = 0
    reconnected = asyncio.Event()

    async def tools(request: web.Request) -> web.WebSocketResponse:
        nonlocal connections
        connections += 1
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        await websocket.receive()
        await websocket.send_json(
            {
                "type": "registered",
                "protocol_version": 1,
                "generation": f"generation-{connections}",
            }
        )
        if connections == 1:
            await websocket.close()
        else:
            reconnected.set()
            await websocket.receive()
        return websocket

    app = web.Application()
    app.router.add_get("/v1/home-assistant/tools", tools)
    test_client = await aiohttp_client(app)
    entry = _entry(str(test_client.make_url("")))
    entry.add_to_hass(hass)
    authority = select_realtime_authority(entry)
    assert authority is not None
    broker = RealtimeToolBroker(
        hass,
        entry,
        authority,
        test_client.session,
        initial_reconnect_delay=0.01,
        max_reconnect_delay=0.02,
    )
    broker.async_start()
    async with asyncio.timeout(2):
        await reconnected.wait()
        while not broker.connected:
            await asyncio.sleep(0)
    await broker.async_stop()
    unregister()

    assert connections == 2
    assert len(api.contexts) == 2
    assert not broker.connected
    assert broker._task is None


async def test_registration_timeout_closes_a_stalled_connection(
    hass: HomeAssistant,
    aiohttp_client: Any,
    socket_enabled: None,
) -> None:
    """A bridge that never registers cannot pin the broker forever."""
    unregister = llm.async_register_api(hass, RecordingAPI(hass, RecordingTool()))

    async def tools(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        await websocket.receive()
        await websocket.receive()
        return websocket

    app = web.Application()
    app.router.add_get("/v1/home-assistant/tools", tools)
    test_client = await aiohttp_client(app)
    entry = _entry(str(test_client.make_url("")))
    authority = select_realtime_authority(entry)
    assert authority is not None
    broker = RealtimeToolBroker(
        hass,
        entry,
        authority,
        test_client.session,
        registration_timeout=0.01,
    )

    with pytest.raises(TimeoutError):
        await broker._async_connect(await broker._async_prepare_registration())
    unregister()
    assert not broker.connected
