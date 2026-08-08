from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bridge.errors import AppServerExited
from bridge.realtime import RealtimeSession


class FakeSubscription:
    def __init__(self) -> None:
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed = False

    async def get(self, timeout: float | None = None) -> dict[str, Any]:
        if timeout is None:
            return await self.events.get()
        return await asyncio.wait_for(self.events.get(), timeout)

    def close(self) -> None:
        self.closed = True


class SdpFirstRpc:
    def __init__(self) -> None:
        self.subscription = FakeSubscription()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def subscribe(self) -> FakeSubscription:
        return self.subscription

    async def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        del timeout
        self.calls.append((method, params))
        if method == "thread/realtime/start":
            thread_id = params["threadId"]
            await self.subscription.events.put(
                {
                    "method": "thread/realtime/sdp",
                    "params": {"threadId": thread_id, "sdp": "v=0\r\nanswer\r\n"},
                }
            )
            await self.subscription.events.put(
                {
                    "method": "thread/realtime/started",
                    "params": {
                        "threadId": thread_id,
                        "realtimeSessionId": "realtime-sdp-first",
                    },
                }
            )
        return {}


class FakePeer:
    def __init__(self) -> None:
        self.answer: str | None = None
        self.connected = False
        self.closed = False

    async def create_offer(self) -> str:
        return "v=0\r\noffer\r\n"

    async def set_answer(self, sdp: str) -> None:
        self.answer = sdp

    async def wait_connected(self, timeout: float | None = None) -> None:
        del timeout
        self.connected = True

    def feed_audio(self, pcm: bytes) -> None:
        del pcm

    async def wait_input_drained(self, timeout: float | None = None) -> None:
        del timeout

    async def recv_audio(self, timeout: float | None = None) -> bytes:
        del timeout
        raise AssertionError("not used")

    async def recv_data_event(self, timeout: float | None = None) -> str | bytes:
        del timeout
        raise AssertionError("not used")

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_start_accepts_sdp_before_started_notification() -> None:
    """App-server may deliver the two handshake notifications in either order."""
    rpc = SdpFirstRpc()
    peer = FakePeer()
    session = RealtimeSession(rpc, "thread-1", peer=peer, timeout=1)

    await session.start()

    assert peer.answer == "v=0\r\nanswer\r\n"
    assert peer.connected is True
    assert session.realtime_session_id == "realtime-sdp-first"
    await session.stop()
    assert peer.closed is True
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
async def test_start_fails_immediately_when_app_server_exits() -> None:
    rpc = SdpFirstRpc()

    async def exit_on_start(
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        del params, timeout
        if method == "thread/realtime/start":
            await rpc.subscription.events.put(
                {"method": "bridge/appServerExited", "params": {"returncode": 17}}
            )
        return {}

    rpc.call = exit_on_start  # type: ignore[method-assign]
    session = RealtimeSession(rpc, "thread-1", peer=FakePeer(), timeout=1)

    with pytest.raises(AppServerExited, match="status 17"):
        await session.start()

    await session.stop()
