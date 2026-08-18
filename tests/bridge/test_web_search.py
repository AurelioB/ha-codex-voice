"""Tests for the bounded subscription-first web-search adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from bridge.errors import ProtocolError
from bridge.web_search import WebSearchBroker, WebSearchUnavailable


@pytest.mark.asyncio
async def test_web_search_is_absent_when_not_configured() -> None:
    broker = WebSearchBroker(None, timeout=1)

    assert broker.enabled is False
    assert broker.tools == ()
    assert broker.owns("search_web") is False
    assert broker.health()["calls_started"] == 0
    await broker.close()


@pytest.mark.asyncio
async def test_web_search_returns_only_bounded_public_results(
    aiohttp_server: Any,
) -> None:
    observed: list[dict[str, str]] = []

    async def handle(request: web.Request) -> web.Response:
        observed.append(dict(request.query))
        return web.json_response(
            {
                "results": [
                    {
                        "title": " Current result ",
                        "url": "https://example.com/current",
                        "content": " A useful   source excerpt. ",
                        "publishedDate": "2026-08-13",
                    },
                    {
                        "title": "Credential URL",
                        "url": "https://user:secret@example.com/private",
                        "content": "must be discarded",
                    },
                    {
                        "title": "Non-web URL",
                        "url": "file:///etc/passwd",
                        "content": "must be discarded",
                    },
                ]
            }
        )

    app = web.Application()
    app.router.add_get("/search", handle)
    server = await aiohttp_server(app)
    broker = WebSearchBroker(str(server.make_url("/search")), timeout=1)
    try:
        result = await broker.call(
            name="search_web",
            arguments={"query": "noticias actuales"},
        )
    finally:
        await broker.close()

    assert result.success is True
    assert result.result == {
        "query": "noticias actuales",
        "results": [
            {
                "title": "Current result",
                "url": "https://example.com/current",
                "snippet": "A useful source excerpt.",
                "published": "2026-08-13",
            }
        ],
    }
    assert observed == [
        {
            "q": "noticias actuales",
            "format": "json",
            "categories": "general",
            "language": "auto",
            "safesearch": "1",
        }
    ]
    assert broker.health()["calls_succeeded"] == 1


@pytest.mark.asyncio
async def test_web_search_uses_codex_subscription_oauth_first(
    aiohttp_server: Any,
    tmp_path: Path,
) -> None:
    observed: list[dict[str, Any]] = []

    async def handle(request: web.Request) -> web.Response:
        observed.append(
            {
                "authorization": request.headers.get("Authorization"),
                "account": request.headers.get("ChatGPT-Account-ID"),
                "originator": request.headers.get("originator"),
                "body": await request.json(),
            }
        )
        return web.json_response(
            {
                "encrypted_output": "opaque",
                "output": "Search result",
                "results": [
                    {
                        "type": "text_result",
                        "title": "Subscription result",
                        "url": "https://example.com/subscription",
                        "snippet": "Current subscription-backed excerpt.",
                    }
                ],
            }
        )

    app = web.Application()
    app.router.add_post("/alpha/search", handle)
    server = await aiohttp_server(app)
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "access-token",
                    "account_id": "account-id",
                }
            }
        )
    )
    broker = WebSearchBroker(
        None,
        timeout=1,
        subscription_auth_file=str(auth_file),
        subscription_endpoint=str(server.make_url("/alpha/search")),
        subscription_model="gpt-test",
    )
    try:
        result = await broker.call(
            name="search_web",
            arguments={"query": "información actual"},
        )
    finally:
        await broker.close()

    assert result.result == {
        "query": "información actual",
        "results": [
            {
                "title": "Subscription result",
                "url": "https://example.com/subscription",
                "snippet": "Current subscription-backed excerpt.",
            }
        ],
    }
    assert observed == [
        {
            "authorization": "Bearer access-token",
            "account": "account-id",
            "originator": "ha_codex_voice",
            "body": {
                "id": observed[0]["body"]["id"],
                "model": "gpt-test",
                "input": "información actual",
                "commands": {
                    "search_query": [{"q": "información actual"}],
                    "response_length": "short",
                },
                "settings": {
                    "allowed_callers": ["direct"],
                    "external_web_access": True,
                    "search_context_size": "low",
                },
                "max_output_tokens": 2_048,
            },
        }
    ]
    assert observed[0]["body"]["id"].startswith("ha-codex-voice-")
    assert broker.health()["primary_backend"] == "codex_subscription"
    assert broker.health()["subscription_calls"] == 1
    assert broker.health()["fallback_calls"] == 0


@pytest.mark.asyncio
async def test_web_search_falls_back_to_local_when_subscription_is_unavailable(
    aiohttp_server: Any,
    tmp_path: Path,
) -> None:
    async def subscription(_request: web.Request) -> web.Response:
        return web.json_response({"error": "unavailable"}, status=503)

    async def local(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "results": [
                    {
                        "title": "Local fallback",
                        "url": "https://example.com/fallback",
                        "content": "Fallback excerpt.",
                    }
                ]
            }
        )

    app = web.Application()
    app.router.add_post("/alpha/search", subscription)
    app.router.add_get("/search", local)
    server = await aiohttp_server(app)
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "access-token",
                    "account_id": "account-id",
                }
            }
        )
    )
    broker = WebSearchBroker(
        str(server.make_url("/search")),
        timeout=1,
        subscription_auth_file=str(auth_file),
        subscription_endpoint=str(server.make_url("/alpha/search")),
    )
    try:
        result = await broker.call(name="search_web", arguments={"query": "current"})
    finally:
        await broker.close()

    assert result.result["results"][0]["title"] == "Local fallback"
    assert broker.health()["subscription_calls"] == 1
    assert broker.health()["fallback_calls"] == 1


@pytest.mark.asyncio
async def test_web_search_rejects_malformed_calls_and_responses(
    aiohttp_server: Any,
) -> None:
    async def handle(_request: web.Request) -> web.Response:
        return web.json_response({"unexpected": []})

    app = web.Application()
    app.router.add_get("/search", handle)
    server = await aiohttp_server(app)
    broker = WebSearchBroker(str(server.make_url("/search")), timeout=1)
    try:
        with pytest.raises(ProtocolError, match="requires exactly query"):
            await broker.call(name="search_web", arguments={})
        with pytest.raises(WebSearchUnavailable, match="no results collection"):
            await broker.call(name="search_web", arguments={"query": "current"})
    finally:
        await broker.close()

    assert broker.health()["calls_started"] == 1
    assert broker.health()["calls_failed"] == 1
