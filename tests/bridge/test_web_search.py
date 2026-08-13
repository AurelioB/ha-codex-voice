"""Tests for the bounded local web-search adapter."""

from __future__ import annotations

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
