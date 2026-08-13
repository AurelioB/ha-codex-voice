"""Tests for the optional external-agent adapter."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web

from bridge.agent_tools import (
    AgentAnnouncementHub,
    AgentAnnouncementUnavailable,
    AgentToolBroker,
    AgentToolUnavailable,
)
from bridge.errors import ProtocolError


@pytest.mark.asyncio
async def test_agent_tools_are_absent_when_not_configured() -> None:
    broker = AgentToolBroker(
        None,
        token=None,
        room="home",
        recall_timeout=1,
        task_timeout=1,
    )

    assert broker.enabled is False
    assert broker.tools == ()
    assert broker.owns("ask_agent") is False
    assert broker.health()["calls_started"] == 0
    await broker.close()


@pytest.mark.asyncio
async def test_agent_announcement_hub_exists_only_during_attachment() -> None:
    hub = AgentAnnouncementHub()
    delivered: list[str] = []

    with pytest.raises(AgentAnnouncementUnavailable, match="no active"):
        await hub.announce("before")

    async def accept(text: str) -> None:
        delivered.append(text)

    async with hub.attach(accept):
        assert hub.health()["active_session"] is True
        await hub.announce("ready")

    assert delivered == ["ready"]
    assert hub.health() == {
        "active_session": False,
        "accepted": 1,
        "unavailable": 1,
    }


@pytest.mark.asyncio
async def test_agent_executes_reference_compatible_recall_and_task_shapes(
    aiohttp_server: Any,
) -> None:
    received: list[tuple[dict[str, Any], str | None]] = []

    async def handle(request: web.Request) -> web.Response:
        payload = await request.json()
        assert isinstance(payload, dict)
        received.append((payload, request.headers.get("Authorization")))
        if "recall" in payload:
            return web.json_response({"matches": ["prefiere español", 3]})
        return web.json_response({"answer": "Trabajo aceptado"})

    app = web.Application()
    app.router.add_post("/agent", handle)
    server = await aiohttp_server(app)
    broker = AgentToolBroker(
        str(server.make_url("/agent")),
        token="agent-secret",
        room="cocina",
        recall_timeout=1,
        task_timeout=1,
    )
    try:
        recall = await broker.call(name="recall_memory", arguments={"query": "idioma"})
        task = await broker.call(
            name="ask_agent", arguments={"question": "Investiga esto"}
        )
    finally:
        await broker.close()

    assert recall.result == {"matches": ["prefiere español"]}
    assert task.result == {"answer": "Trabajo aceptado"}
    assert received == [
        ({"recall": "idioma"}, "Bearer agent-secret"),
        (
            {"question": "Investiga esto", "room": "cocina"},
            "Bearer agent-secret",
        ),
    ]
    health = broker.health()
    assert health["enabled"] is True
    assert health["calls_started"] == 2
    assert health["calls_succeeded"] == 2
    assert health["calls_failed"] == 0
    assert isinstance(health["last_call_duration_ms"], int)


@pytest.mark.asyncio
async def test_agent_rejects_undeclared_or_malformed_calls() -> None:
    broker = AgentToolBroker(
        "http://agent.invalid/task",
        token=None,
        room="home",
        recall_timeout=1,
        task_timeout=1,
    )
    try:
        with pytest.raises(ProtocolError, match="undeclared"):
            await broker.call(name="HassTurnOn", arguments={})
        with pytest.raises(ProtocolError, match="requires exactly question"):
            await broker.call(name="ask_agent", arguments={})
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_agent_fails_closed_on_invalid_response(aiohttp_server: Any) -> None:
    async def handle(_request: web.Request) -> web.Response:
        return web.json_response({"unexpected": True})

    app = web.Application()
    app.router.add_post("/agent", handle)
    server = await aiohttp_server(app)
    broker = AgentToolBroker(
        str(server.make_url("/agent")),
        token=None,
        room="home",
        recall_timeout=1,
        task_timeout=1,
    )
    try:
        with pytest.raises(AgentToolUnavailable, match="no answer"):
            await broker.call(name="ask_agent", arguments={"question": "x"})
    finally:
        await broker.close()

    assert broker.health()["calls_failed"] == 1
