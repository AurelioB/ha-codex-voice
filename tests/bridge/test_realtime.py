from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bridge import realtime as realtime_module
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


class BlockingDrainPeer(FakePeer):
    def __init__(self) -> None:
        super().__init__()
        self.drain_started = asyncio.Event()
        self.release_drain = asyncio.Event()
        self.drain_cancelled = False

    async def wait_input_drained(self, timeout: float | None = None) -> None:
        self.drain_started.set()
        try:
            if timeout is None:
                await self.release_drain.wait()
            else:
                await asyncio.wait_for(self.release_drain.wait(), timeout)
        except asyncio.CancelledError:
            self.drain_cancelled = True
            raise


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class AdvancingSubscription(FakeSubscription):
    def __init__(self, clock: FakeMonotonic) -> None:
        super().__init__()
        self.clock = clock
        self.timeouts: list[float | None] = []

    async def get(self, timeout: float | None = None) -> dict[str, Any]:
        self.timeouts.append(timeout)
        if len(self.timeouts) > 3:
            raise AssertionError("unrelated events reset the handshake timeout")
        self.clock.now += 0.4
        return {
            "method": "thread/realtime/progress",
            "params": {"threadId": "unrelated-thread"},
        }


class DeadlineRpc(SdpFirstRpc):
    def __init__(self, clock: FakeMonotonic) -> None:
        super().__init__()
        self.subscription = AdvancingSubscription(clock)

    async def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        del method, params, timeout
        return {}


class BacklogRpc(SdpFirstRpc):
    async def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        del timeout
        if method == "thread/realtime/start":
            thread_id = params["threadId"]
            await self.subscription.events.put(
                {
                    "method": "thread/realtime/started",
                    "params": {
                        "threadId": thread_id,
                        "realtimeSessionId": "realtime-with-backlog",
                    },
                }
            )
            for sequence in range(70):
                await self.subscription.events.put(
                    {
                        "method": "thread/realtime/progress",
                        "params": {"threadId": thread_id, "sequence": sequence},
                    }
                )
            await self.subscription.events.put(
                {
                    "method": "thread/realtime/sdp",
                    "params": {"threadId": thread_id, "sdp": "v=0\r\nanswer\r\n"},
                }
            )
        return {}


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


@pytest.mark.asyncio
async def test_start_uses_one_deadline_across_unrelated_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeMonotonic()
    monkeypatch.setattr(realtime_module, "monotonic", clock)
    rpc = DeadlineRpc(clock)
    session = RealtimeSession(rpc, "thread-1", peer=FakePeer(), timeout=1)

    with pytest.raises(TimeoutError, match="realtime handshake timed out"):
        await session.start()

    assert rpc.subscription.timeouts == pytest.approx([1.0, 0.6, 0.2])
    await session.stop()


@pytest.mark.asyncio
async def test_start_caps_same_thread_pre_handshake_backlog() -> None:
    rpc = BacklogRpc()
    session = RealtimeSession(rpc, "thread-1", peer=FakePeer(), timeout=1)

    await session.start()

    assert len(session._backlog) == 64
    assert [event["params"]["sequence"] for event in session._backlog] == list(
        range(6, 70)
    )
    await session.stop()


@pytest.mark.asyncio
async def test_wait_input_drained_detects_exit_and_bounds_rebuffered_events() -> None:
    rpc = SdpFirstRpc()
    peer = BlockingDrainPeer()
    session = RealtimeSession(rpc, "thread-1", peer=peer, timeout=1)
    drain = asyncio.create_task(session.wait_input_drained())
    await peer.drain_started.wait()

    for sequence in range(70):
        await rpc.subscription.events.put(
            {
                "method": "thread/realtime/progress",
                "params": {"threadId": "thread-1", "sequence": sequence},
            }
        )
    await rpc.subscription.events.put(
        {
            "method": "thread/realtime/progress",
            "params": {"threadId": "unrelated-thread", "sequence": 999},
        }
    )
    await rpc.subscription.events.put(
        {"method": "bridge/appServerExited", "params": {"returncode": 23}}
    )

    with pytest.raises(AppServerExited, match="status 23"):
        await drain

    assert peer.drain_cancelled is True
    assert len(session._backlog) == 64
    assert [event["params"]["sequence"] for event in session._backlog] == list(
        range(6, 70)
    )
    await session.stop()
