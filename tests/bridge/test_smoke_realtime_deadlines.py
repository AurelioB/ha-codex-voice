"""Bridge-suite regressions for absolute realtime smoke-client deadlines."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, Self

import pytest

from scripts import smoke_realtime, smoke_realtime_v2

SmokeRunner = Callable[[str, str, str], Coroutine[Any, Any, dict[str, Any]]]


def _started_event(*, protocol_version: int) -> dict[str, Any]:
    if protocol_version == 1:
        return {"type": "started"}
    return {
        "type": "started",
        "protocol_version": 2,
        "conversation_mode": "native",
        "output_sample_rate": 24_000,
        "output_channels": 1,
        "capabilities": {
            "binary_pcm16": True,
            "local_flush": True,
            "remote_cancel": False,
            "same_session_interrupt_ack": True,
        },
    }


class _HeartbeatWebSocket:
    """Emulate aiohttp consuming internal heartbeat frames forever."""

    def __init__(self, started: dict[str, Any] | None) -> None:
        self._started = started
        self.heartbeat_count = 0
        self.receive_json_timeouts: list[float | None] = []
        self.receive_timeouts: list[float | None] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def send_json(self, _payload: dict[str, Any]) -> None:
        return None

    async def _consume_heartbeats(self) -> None:
        while True:
            self.heartbeat_count += 1
            await asyncio.sleep(0.005)

    async def receive_json(self, *, timeout: float | None = None) -> dict[str, Any]:
        self.receive_json_timeouts.append(timeout)
        if self._started is not None:
            return self._started
        await self._consume_heartbeats()
        raise AssertionError("unreachable")

    async def receive(self, timeout: float | None = None) -> Any:
        self.receive_timeouts.append(timeout)
        await self._consume_heartbeats()
        raise AssertionError("unreachable")


class _HangingConnectWebSocket(_HeartbeatWebSocket):
    """Emulate a WebSocket handshake that never completes."""

    async def __aenter__(self) -> Self:
        await self._consume_heartbeats()
        raise AssertionError("unreachable")


class _FakeClientSession:
    def __init__(self, websocket: _HeartbeatWebSocket) -> None:
        self._websocket = websocket

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def ws_connect(self, _url: str) -> _HeartbeatWebSocket:
        return self._websocket


def _install_fake_session(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    websocket: _HeartbeatWebSocket,
) -> None:
    def session_factory(**_kwargs: object) -> _FakeClientSession:
        return _FakeClientSession(websocket)

    monkeypatch.setattr(module, "ClientSession", session_factory)


@pytest.mark.parametrize(
    ("runner", "module", "expected_error"),
    [
        (smoke_realtime.run_smoke, smoke_realtime, "realtime start timed out"),
        (
            smoke_realtime_v2.run_smoke,
            smoke_realtime_v2,
            "realtime v2 start timed out",
        ),
    ],
)
async def test_handshake_deadline_is_not_extended_by_heartbeats(
    monkeypatch: pytest.MonkeyPatch,
    runner: SmokeRunner,
    module: Any,
    expected_error: str,
) -> None:
    """Heartbeat traffic must not extend the handshake deadline."""
    websocket = _HeartbeatWebSocket(started=None)
    _install_fake_session(monkeypatch, module, websocket)
    monkeypatch.setattr(module, "_HANDSHAKE_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(TimeoutError, match=f"^{expected_error}$"):
        async with asyncio.timeout(0.25):
            await runner("http://bridge.test", "test-token", "hello")

    assert websocket.receive_json_timeouts == [None]
    assert websocket.heartbeat_count > 1


@pytest.mark.parametrize(
    ("runner", "module", "expected_error"),
    [
        (smoke_realtime.run_smoke, smoke_realtime, "realtime start timed out"),
        (
            smoke_realtime_v2.run_smoke,
            smoke_realtime_v2,
            "realtime v2 start timed out",
        ),
    ],
)
async def test_handshake_deadline_includes_hanging_websocket_connect(
    monkeypatch: pytest.MonkeyPatch,
    runner: SmokeRunner,
    module: Any,
    expected_error: str,
) -> None:
    """A stalled WebSocket connection must consume the handshake budget."""
    websocket = _HangingConnectWebSocket(started=None)
    _install_fake_session(monkeypatch, module, websocket)
    monkeypatch.setattr(module, "_HANDSHAKE_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(TimeoutError, match=f"^{expected_error}$"):
        async with asyncio.timeout(0.25):
            await runner("http://bridge.test", "test-token", "hello")

    assert websocket.receive_json_timeouts == []
    assert websocket.heartbeat_count > 1


@pytest.mark.parametrize(
    ("runner", "module", "protocol_version", "expected_error"),
    [
        (
            smoke_realtime.run_smoke,
            smoke_realtime,
            1,
            "realtime audio/transcript timed out",
        ),
        (
            smoke_realtime_v2.run_smoke,
            smoke_realtime_v2,
            2,
            "realtime v2 output timed out",
        ),
    ],
)
async def test_output_deadline_is_not_extended_by_heartbeats(
    monkeypatch: pytest.MonkeyPatch,
    runner: SmokeRunner,
    module: Any,
    protocol_version: int,
    expected_error: str,
) -> None:
    """Heartbeat traffic must not extend the output deadline."""
    websocket = _HeartbeatWebSocket(
        started=_started_event(protocol_version=protocol_version)
    )
    _install_fake_session(monkeypatch, module, websocket)
    monkeypatch.setattr(module, "_OUTPUT_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(TimeoutError, match=f"^{expected_error}$"):
        async with asyncio.timeout(0.25):
            await runner("http://bridge.test", "test-token", "hello")

    assert websocket.receive_json_timeouts == [None]
    assert websocket.receive_timeouts == [None]
    assert websocket.heartbeat_count > 1
