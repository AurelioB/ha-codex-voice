from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bridge.errors import BridgeBusyError, ProtocolError
from bridge.tool_broker import (
    HomeAssistantToolBroker,
    ToolBrokerUnavailable,
    _validated_registration,
)


class FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def send_json(self, value: dict[str, Any]) -> None:
        if self.closed:
            raise ConnectionError("closed")
        await self.sent.put(value)


class BlockingToolCallWebSocket(FakeWebSocket):
    """Accept registration, then simulate a permanently backpressured write."""

    def __init__(self) -> None:
        super().__init__()
        self._send_count = 0

    async def send_json(self, value: dict[str, Any]) -> None:
        self._send_count += 1
        if self._send_count == 1:
            await super().send_json(value)
            return
        await asyncio.Event().wait()


def _registration(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "type": "register",
        "protocol_version": 1,
        "authority_id": "entry:conversation",
        "language": "es-MX",
        "voice": "cove",
        "timezone": "America/Mexico_City",
        "location": "Casa",
        "latitude": 19.4326,
        "longitude": -99.1332,
        "instructions": "Only control exposed Home Assistant entities.",
        "tools": [
            {
                "name": "HassTurnOn",
                "description": "Turn on an exposed entity",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            }
        ],
    }
    value.update(overrides)
    return value


async def test_registration_and_correlated_tool_result() -> None:
    broker = HomeAssistantToolBroker()
    websocket = FakeWebSocket()
    snapshot = await broker.register(websocket, _registration())  # type: ignore[arg-type]
    registered = await websocket.sent.get()
    assert registered == {
        "type": "registered",
        "protocol_version": 1,
        "generation": snapshot.generation,
    }
    assert snapshot.language == "es-MX"
    assert snapshot.voice == "cove"
    assert snapshot.timezone == "America/Mexico_City"
    assert snapshot.location == "Casa"
    assert snapshot.latitude == 19.4326
    assert snapshot.longitude == -99.1332
    assert snapshot.tool_names == {"HassTurnOn"}

    call = asyncio.create_task(
        broker.call(snapshot, name="HassTurnOn", arguments={"name": "la luz"})
    )
    request = await websocket.sent.get()
    assert request["type"] == "tool_call"
    assert request["generation"] == snapshot.generation
    assert request["name"] == "HassTurnOn"
    assert request["arguments"] == {"name": "la luz"}
    await broker.handle_message(  # type: ignore[arg-type]
        websocket,
        {
            "type": "tool_result",
            "generation": snapshot.generation,
            "call_id": request["call_id"],
            "success": True,
            "result": {"speech": "Encendí la luz"},
        },
    )
    result = await call
    assert result.success is True
    assert result.result == {"speech": "Encendí la luz"}
    health = broker.health()
    assert health["connected"] is True
    assert health["voice"] == "cove"
    assert health["local_context_available"] is True
    assert health["tool_count"] == 1
    assert health["pending_calls"] == 0
    assert health["calls_started"] == 1
    assert health["calls_succeeded"] == 1
    assert health["calls_failed"] == 0
    assert health["calls_timed_out"] == 0
    assert isinstance(health["last_call_duration_ms"], int)


async def test_tool_deadline_includes_stalled_broker_write() -> None:
    broker = HomeAssistantToolBroker(timeout=0.01)
    websocket = BlockingToolCallWebSocket()
    snapshot = await broker.register(websocket, _registration())  # type: ignore[arg-type]
    await websocket.sent.get()

    with pytest.raises(ToolBrokerUnavailable, match="outcome unknown"):
        await asyncio.wait_for(
            broker.call(snapshot, name="HassTurnOn", arguments={}), timeout=0.2
        )

    health = broker.health()
    assert health["pending_calls"] == 0
    assert health["calls_started"] == 1
    assert health["calls_succeeded"] == 0
    assert health["calls_failed"] == 0
    assert health["calls_timed_out"] == 1
    assert health["calls_transport_failed"] == 0
    assert health["calls_cancelled"] == 0


async def test_tool_call_fails_closed_for_undeclared_name() -> None:
    broker = HomeAssistantToolBroker()
    websocket = FakeWebSocket()
    snapshot = await broker.register(websocket, _registration())  # type: ignore[arg-type]
    await websocket.sent.get()

    with pytest.raises(ProtocolError, match="undeclared"):
        await broker.call(snapshot, name="HassUnlock", arguments={})


async def test_disconnect_marks_in_flight_outcome_unknown() -> None:
    broker = HomeAssistantToolBroker()
    websocket = FakeWebSocket()
    snapshot = await broker.register(websocket, _registration())  # type: ignore[arg-type]
    await websocket.sent.get()
    call = asyncio.create_task(
        broker.call(snapshot, name="HassTurnOn", arguments={"name": "office"})
    )
    await websocket.sent.get()

    await broker.unregister(websocket)  # type: ignore[arg-type]
    with pytest.raises(ToolBrokerUnavailable, match="outcome unknown"):
        await call
    assert broker.snapshot is None


async def test_cancelled_call_consumes_one_late_result_without_reexecution() -> None:
    broker = HomeAssistantToolBroker()
    websocket = FakeWebSocket()
    snapshot = await broker.register(websocket, _registration())  # type: ignore[arg-type]
    await websocket.sent.get()
    call = asyncio.create_task(
        broker.call(snapshot, name="HassTurnOn", arguments={"name": "office"})
    )
    request = await websocket.sent.get()

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    late_result = {
        "type": "tool_result",
        "generation": snapshot.generation,
        "call_id": request["call_id"],
        "success": True,
        "result": {"speech": "late"},
    }
    await broker.handle_message(websocket, late_result)  # type: ignore[arg-type]

    with pytest.raises(ProtocolError, match="unknown or stale"):
        await broker.handle_message(websocket, late_result)  # type: ignore[arg-type]


async def test_timed_out_call_consumes_late_result_without_losing_authority() -> None:
    broker = HomeAssistantToolBroker(timeout=0.001)
    websocket = FakeWebSocket()
    snapshot = await broker.register(websocket, _registration())  # type: ignore[arg-type]
    await websocket.sent.get()
    call = asyncio.create_task(
        broker.call(snapshot, name="HassTurnOn", arguments={"name": "office"})
    )
    request = await websocket.sent.get()

    with pytest.raises(ToolBrokerUnavailable, match="outcome unknown"):
        await call
    await broker.handle_message(  # type: ignore[arg-type]
        websocket,
        {
            "type": "tool_result",
            "generation": snapshot.generation,
            "call_id": request["call_id"],
            "success": True,
            "result": {"speech": "late"},
        },
    )

    assert broker.snapshot is snapshot


async def test_second_live_authority_is_rejected() -> None:
    broker = HomeAssistantToolBroker()
    first = FakeWebSocket()
    second = FakeWebSocket()
    await broker.register(first, _registration())  # type: ignore[arg-type]

    with pytest.raises(BridgeBusyError, match="already connected"):
        await broker.register(second, _registration())  # type: ignore[arg-type]


async def test_closed_authority_replacement_fails_old_pending_generation() -> None:
    broker = HomeAssistantToolBroker()
    first = FakeWebSocket()
    snapshot = await broker.register(first, _registration())  # type: ignore[arg-type]
    await first.sent.get()
    call = asyncio.create_task(
        broker.call(snapshot, name="HassTurnOn", arguments={"name": "office"})
    )
    await first.sent.get()
    first.closed = True

    second = FakeWebSocket()
    replacement = await broker.register(second, _registration())  # type: ignore[arg-type]

    assert replacement.generation != snapshot.generation
    with pytest.raises(ToolBrokerUnavailable, match="outcome unknown"):
        await call


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"protocol_version": 2}, "unsupported"),
        ({"language": "es MX"}, "BCP-47"),
        ({"timezone": "Mars/Olympus"}, "valid IANA timezone"),
        ({"longitude": None}, "coordinates must be paired"),
        ({"tools": "HassTurnOn"}, "bounded list"),
        (
            {"tools": [{"name": "bad name", "description": "", "parameters": {}}]},
            "name is invalid",
        ),
        (
            {
                "tools": [
                    {"name": "Same", "description": "", "parameters": {}},
                    {"name": "Same", "description": "", "parameters": {}},
                ]
            },
            "duplicate",
        ),
    ],
)
def test_registration_rejects_untrusted_or_ambiguous_values(
    override: dict[str, Any], match: str
) -> None:
    with pytest.raises(ProtocolError, match=match):
        _validated_registration(_registration(**override))


def test_registration_canonicalizes_language_and_rejects_non_finite_json() -> None:
    assert _validated_registration(_registration(language="ES-mx")).language == "es-MX"

    with pytest.raises(ProtocolError, match="not JSON"):
        _validated_registration(
            _registration(
                tools=[
                    {
                        "name": "UnsafeSchema",
                        "description": "",
                        "parameters": {"default": float("nan")},
                    }
                ]
            )
        )


async def test_stale_and_duplicate_results_fail_closed() -> None:
    broker = HomeAssistantToolBroker()
    websocket = FakeWebSocket()
    snapshot = await broker.register(websocket, _registration())  # type: ignore[arg-type]
    await websocket.sent.get()

    with pytest.raises(ProtocolError, match="correlation"):
        await broker.handle_message(  # type: ignore[arg-type]
            websocket,
            {
                "type": "tool_result",
                "generation": "stale",
                "call_id": "missing",
                "success": True,
                "result": {},
            },
        )

    with pytest.raises(ProtocolError, match="unknown or stale"):
        await broker.handle_message(  # type: ignore[arg-type]
            websocket,
            {
                "type": "tool_result",
                "generation": snapshot.generation,
                "call_id": "missing",
                "success": True,
                "result": {},
            },
        )
