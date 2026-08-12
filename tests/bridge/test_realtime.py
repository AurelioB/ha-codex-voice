from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bridge import realtime as realtime_module
from bridge.errors import AppServerExited, ProtocolError
from bridge.realtime import RealtimeSession, SignalingRealtimeSession


class FakeSubscription:
    def __init__(self) -> None:
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed = False

    async def get(self, timeout: float | None = None) -> dict[str, Any]:
        if timeout is None:
            return await self.events.get()
        return await asyncio.wait_for(self.events.get(), timeout)

    def get_nowait(self) -> dict[str, Any]:
        return self.events.get_nowait()

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
        self.sent_data_events: list[str | bytes] = []

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

    def send_data_event(self, value: str | bytes) -> None:
        self.sent_data_events.append(value)

    async def close(self) -> None:
        self.closed = True


class ControlledStopRpc(SdpFirstRpc):
    def __init__(
        self,
        *,
        block_stop: bool = False,
        stop_error: Exception | None = None,
        emit_closed_on_stop: bool = False,
    ) -> None:
        super().__init__()
        self.block_stop = block_stop
        self.stop_error = stop_error
        self.emit_closed_on_stop = emit_closed_on_stop
        self.stop_started = asyncio.Event()
        self.release_stop = asyncio.Event()
        self.stop_calls = 0
        self.stop_cancelled = False
        self.stop_timeouts: list[float | None] = []

    async def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if method != "thread/realtime/stop":
            return await super().call(method, params, timeout=timeout)
        self.calls.append((method, params))
        self.stop_calls += 1
        self.stop_timeouts.append(timeout)
        self.stop_started.set()
        try:
            if self.stop_error is not None:
                raise self.stop_error
            if self.block_stop:
                await self.release_stop.wait()
            if self.emit_closed_on_stop:
                await self.subscription.events.put(
                    {
                        "method": "thread/realtime/closed",
                        "params": {"threadId": params["threadId"]},
                    }
                )
        except asyncio.CancelledError:
            self.stop_cancelled = True
            raise
        return {}


class ControlledClosePeer(FakePeer):
    def __init__(
        self,
        *,
        block_close: bool = False,
        close_error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.block_close = block_close
        self.close_error = close_error
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_calls = 0
        self.close_cancelled = False

    async def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        try:
            if self.close_error is not None:
                raise self.close_error
            if self.block_close:
                await self.release_close.wait()
            self.closed = True
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise


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
@pytest.mark.parametrize("version", ["v1", "v3"])
async def test_signaling_session_relays_exact_external_offer_and_answer(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
) -> None:
    def local_peer_must_not_be_created() -> Any:
        raise AssertionError("signaling-only session constructed a local peer")

    monkeypatch.setattr(realtime_module, "WebRtcPeer", local_peer_must_not_be_created)
    rpc = SdpFirstRpc()
    session = SignalingRealtimeSession(rpc, "thread-1", version=version, timeout=1)
    offer = "v=0\r\na=device-owned-offer:exact whitespace \r\n"

    answer = await session.start(
        offer,
        prompt="Responde en español de México.",
        voice="cove",
    )

    assert answer == "v=0\r\nanswer\r\n"
    assert session.realtime_session_id == "realtime-sdp-first"
    assert rpc.calls[0] == (
        "thread/realtime/start",
        {
            "threadId": "thread-1",
            "outputModality": "audio",
            "includeStartupContext": False,
            "clientManagedHandoffs": False,
            "transport": {"type": "webrtc", "sdp": offer},
            "version": version,
            "prompt": "Responde en español de México.",
            "voice": "cove",
        },
    )
    await session.stop()
    assert rpc.calls[-1] == (
        "thread/realtime/stop",
        {"threadId": "thread-1"},
    )
    assert rpc.subscription.closed is True


@pytest.mark.parametrize("version", ["", "v2", "V3"])
def test_signaling_session_rejects_unsupported_version(version: str) -> None:
    class SubscribeMustNotRun:
        def subscribe(self) -> None:
            raise AssertionError("invalid version created a subscription")

    with pytest.raises(ProtocolError, match="WebRTC realtime version must be v1 or v3"):
        SignalingRealtimeSession(SubscribeMustNotRun(), "thread-1", version=version)


@pytest.mark.asyncio
async def test_signaling_session_accepts_started_before_sdp_and_preserves_events() -> (
    None
):
    rpc = SdpFirstRpc()

    async def started_first(
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        del timeout
        rpc.calls.append((method, params))
        if method == "thread/realtime/start":
            await rpc.subscription.events.put(
                {
                    "method": "thread/realtime/started",
                    "params": {
                        "threadId": "thread-1",
                        "realtimeSessionId": "realtime-started-first",
                    },
                }
            )
            await rpc.subscription.events.put(
                {
                    "method": "thread/realtime/progress",
                    "params": {"threadId": "other-thread", "sequence": -1},
                }
            )
            await rpc.subscription.events.put(
                {
                    "method": "thread/realtime/progress",
                    "params": {"threadId": "thread-1", "sequence": 1},
                }
            )
            await rpc.subscription.events.put(
                {
                    "method": "thread/realtime/sdp",
                    "params": {
                        "threadId": "thread-1",
                        "sdp": " exact-answer-without-normalization ",
                    },
                }
            )
        return {}

    rpc.call = started_first  # type: ignore[method-assign]
    session = SignalingRealtimeSession(rpc, "thread-1", timeout=1)

    assert await session.start("exact-offer") == (
        " exact-answer-without-normalization "
    )
    assert session.realtime_session_id == "realtime-started-first"
    assert (await session.next_event())["method"] == "thread/realtime/started"
    assert (await session.next_event())["params"]["sequence"] == 1
    await session.stop()


@pytest.mark.asyncio
async def test_signaling_session_rejects_invalid_offer_before_remote_start() -> None:
    rpc = SdpFirstRpc()
    session = SignalingRealtimeSession(rpc, "thread-1", timeout=1)

    with pytest.raises(ProtocolError, match="invalid SDP offer"):
        await session.start("")

    assert rpc.calls == []
    await session.stop()
    assert rpc.calls == []
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
async def test_signaling_session_is_single_start_even_after_handshake_failure() -> None:
    rpc = SdpFirstRpc()

    async def fail_start(
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        del params, timeout
        if method == "thread/realtime/start":
            raise RuntimeError("unknown remote start outcome")
        rpc.calls.append((method, {"threadId": "thread-1"}))
        return {}

    rpc.call = fail_start  # type: ignore[method-assign]
    session = SignalingRealtimeSession(rpc, "thread-1", timeout=1)

    with pytest.raises(RuntimeError, match="unknown remote start outcome"):
        await session.start("offer")
    with pytest.raises(ProtocolError, match="already been started"):
        await session.start("offer-again")

    await session.stop()
    assert rpc.calls == [("thread/realtime/stop", {"threadId": "thread-1"})]


@pytest.mark.asyncio
async def test_signaling_session_rejects_provider_error_and_conflicting_answers() -> (
    None
):
    rpc = SdpFirstRpc()

    async def provider_error(
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        del timeout
        rpc.calls.append((method, params))
        if method == "thread/realtime/start":
            await rpc.subscription.events.put(
                {
                    "method": "thread/realtime/error",
                    "params": {"threadId": "thread-1", "message": "offer rejected"},
                }
            )
        return {}

    rpc.call = provider_error  # type: ignore[method-assign]
    failed = SignalingRealtimeSession(rpc, "thread-1", timeout=1)
    with pytest.raises(ProtocolError, match="realtime provider error"):
        await failed.start("offer")
    await failed.stop()

    conflicting_rpc = SdpFirstRpc()

    async def conflicting_answers(
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        del timeout
        conflicting_rpc.calls.append((method, params))
        if method == "thread/realtime/start":
            for answer in ("answer-1", "answer-2"):
                await conflicting_rpc.subscription.events.put(
                    {
                        "method": "thread/realtime/sdp",
                        "params": {"threadId": "thread-1", "sdp": answer},
                    }
                )
            await conflicting_rpc.subscription.events.put(
                {
                    "method": "thread/realtime/started",
                    "params": {"threadId": "thread-1"},
                }
            )
        return {}

    conflicting_rpc.call = conflicting_answers  # type: ignore[method-assign]
    conflicting = SignalingRealtimeSession(conflicting_rpc, "thread-1", timeout=1)
    with pytest.raises(ProtocolError, match="conflicting SDP answers"):
        await conflicting.start("offer")
    await conflicting.stop()


@pytest.mark.asyncio
async def test_signaling_session_rejects_provider_close_during_handshake() -> None:
    rpc = SdpFirstRpc()

    async def provider_closed(
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        del timeout
        rpc.calls.append((method, params))
        if method == "thread/realtime/start":
            await rpc.subscription.events.put(
                {
                    "method": "thread/realtime/closed",
                    "params": {"threadId": "thread-1"},
                }
            )
        return {}

    rpc.call = provider_closed  # type: ignore[method-assign]
    session = SignalingRealtimeSession(rpc, "thread-1", timeout=1)

    with pytest.raises(
        ProtocolError, match="realtime provider closed during signaling"
    ):
        await session.start("offer")

    await session.stop()


@pytest.mark.asyncio
async def test_signaling_session_rejects_late_conflicting_answer() -> None:
    rpc = SdpFirstRpc()
    session = SignalingRealtimeSession(rpc, "thread-1", timeout=1)
    await session.start("offer")
    assert (await session.next_event())["method"] == "thread/realtime/started"
    await rpc.subscription.events.put(
        {
            "method": "thread/realtime/sdp",
            "params": {"threadId": "thread-1", "sdp": "different-answer"},
        }
    )

    with pytest.raises(ProtocolError, match="conflicting SDP answers"):
        await session.next_event()

    await session.stop()


@pytest.mark.asyncio
async def test_signaling_session_uses_one_deadline_across_unrelated_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeMonotonic()
    monkeypatch.setattr(realtime_module, "monotonic", clock)
    rpc = DeadlineRpc(clock)
    session = SignalingRealtimeSession(rpc, "thread-1", timeout=1)

    with pytest.raises(TimeoutError, match="realtime signaling timed out"):
        await session.start("offer")

    assert rpc.subscription.timeouts == pytest.approx([1.0, 0.6, 0.2])
    await session.stop()


@pytest.mark.asyncio
async def test_signaling_session_hard_bounds_rpc_that_ignores_timeout() -> None:
    rpc = SdpFirstRpc()
    start_cancelled = False

    async def hang_start(
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        nonlocal start_cancelled
        del params, timeout
        if method == "thread/realtime/start":
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                start_cancelled = True
                raise
        return {}

    rpc.call = hang_start  # type: ignore[method-assign]
    session = SignalingRealtimeSession(rpc, "thread-1", timeout=0.02)

    with pytest.raises(TimeoutError, match="realtime signaling timed out"):
        await asyncio.wait_for(session.start("offer"), timeout=0.2)

    assert start_cancelled is True
    await session.stop()
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
async def test_signaling_next_event_detects_app_server_exit() -> None:
    rpc = SdpFirstRpc()
    session = SignalingRealtimeSession(rpc, "thread-1", timeout=1)
    await session.start("offer")
    assert (await session.next_event())["method"] == "thread/realtime/started"
    await rpc.subscription.events.put(
        {"method": "bridge/appServerExited", "params": {"returncode": 29}}
    )

    with pytest.raises(AppServerExited, match="status 29"):
        await session.next_event()

    await session.stop()


@pytest.mark.asyncio
async def test_signaling_next_event_uses_one_deadline_across_unrelated_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeMonotonic()
    monkeypatch.setattr(realtime_module, "monotonic", clock)
    rpc = DeadlineRpc(clock)
    session = SignalingRealtimeSession(rpc, "thread-1", timeout=1)

    with pytest.raises(TimeoutError):
        await session.next_event(timeout=1)

    assert rpc.subscription.timeouts == pytest.approx([1.0, 0.6, 0.2])
    await session.stop()


@pytest.mark.asyncio
async def test_signaling_stop_is_idempotent_and_survives_caller_cancellation() -> None:
    rpc = ControlledStopRpc(block_stop=True)
    session = SignalingRealtimeSession(rpc, "thread-1", timeout=1)
    session._start_requested = True
    caller = asyncio.create_task(session.stop())
    await asyncio.wait_for(rpc.stop_started.wait(), timeout=0.2)

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert rpc.stop_cancelled is False

    second = asyncio.create_task(session.stop())
    rpc.release_stop.set()
    await second
    await session.stop()

    assert rpc.stop_calls == 1
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
async def test_signaling_stop_bounds_hung_remote_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(realtime_module, "_STOP_TIMEOUT_SECONDS", 0.02)
    rpc = ControlledStopRpc(block_stop=True)
    session = SignalingRealtimeSession(rpc, "thread-1", timeout=1)
    session._start_requested = True

    await asyncio.wait_for(session.stop(), timeout=0.5)

    assert rpc.stop_calls == 1
    assert rpc.stop_cancelled is True
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
async def test_signaling_strict_stop_success_is_reusable_thread_boundary() -> None:
    rpc = ControlledStopRpc(emit_closed_on_stop=True)
    session = SignalingRealtimeSession(rpc, "thread-1", timeout=1)
    session._start_requested = True
    rpc.subscription.events.put_nowait(
        {
            "method": "thread/realtime/progress",
            "params": {"threadId": "thread-1", "sequence": "before-closed"},
        }
    )

    await session.stop_strict()
    await session.stop_strict()

    assert rpc.stop_calls == 1
    assert rpc.subscription.closed is True
    assert rpc.subscription.events.empty()


@pytest.mark.asyncio
async def test_signaling_strict_stop_waiter_cancellation_rejoins_one_stop() -> None:
    rpc = ControlledStopRpc(block_stop=True, emit_closed_on_stop=True)
    session = SignalingRealtimeSession(rpc, "thread-1", timeout=1)
    session._start_requested = True
    strict_waiter = asyncio.create_task(session.stop_strict())
    await asyncio.wait_for(rpc.stop_started.wait(), timeout=0.2)

    strict_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await strict_waiter
    assert rpc.stop_cancelled is False

    retired_cleanup = asyncio.create_task(session.stop())
    await asyncio.sleep(0)
    assert not retired_cleanup.done()
    rpc.release_stop.set()
    await retired_cleanup
    await session.stop()

    assert rpc.stop_calls == 1
    assert rpc.stop_cancelled is False
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
async def test_signaling_strict_stop_rpc_return_without_closed_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(realtime_module, "_STOP_TIMEOUT_SECONDS", 0.02)
    rpc = ControlledStopRpc()
    session = SignalingRealtimeSession(rpc, "thread-1", timeout=1)
    session._start_requested = True

    with pytest.raises(
        TimeoutError, match="realtime signaling stop outcome is ambiguous"
    ):
        await session.stop_strict()

    assert rpc.stop_calls == 1
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
async def test_signaling_strict_stop_propagates_error_without_second_attempt() -> None:
    rpc = ControlledStopRpc(stop_error=RuntimeError("unknown stop outcome"))
    session = SignalingRealtimeSession(rpc, "thread-1", timeout=1)
    session._start_requested = True

    with pytest.raises(RuntimeError, match="unknown stop outcome"):
        await session.stop_strict()
    await session.stop()

    assert rpc.stop_calls == 1
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
async def test_signaling_strict_stop_propagates_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(realtime_module, "_STOP_TIMEOUT_SECONDS", 0.02)
    rpc = ControlledStopRpc(block_stop=True)
    session = SignalingRealtimeSession(rpc, "thread-1", timeout=1)
    session._start_requested = True

    with pytest.raises(
        TimeoutError, match="realtime signaling stop outcome is ambiguous"
    ):
        await asyncio.wait_for(session.stop_strict(), timeout=0.5)

    assert rpc.stop_calls == 1
    assert rpc.stop_cancelled is True
    assert rpc.subscription.closed is True


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
async def test_v3_start_can_disable_delegation_ack_filler() -> None:
    rpc = SdpFirstRpc()
    session = RealtimeSession(rpc, "thread-1", peer=FakePeer(), timeout=1)

    await session.start(delegation_ack_filler=False)

    method, params = rpc.calls[0]
    assert method == "thread/realtime/start"
    assert params["delegationAckFiller"] is False
    await session.stop()


@pytest.mark.asyncio
async def test_v1_start_rejects_delegation_ack_filler_control() -> None:
    rpc = SdpFirstRpc()
    session = RealtimeSession(
        rpc,
        "thread-1",
        peer=FakePeer(),
        version="v1",
        timeout=1,
    )

    with pytest.raises(
        ProtocolError, match="delegation acknowledgement control requires version v3"
    ):
        await session.start(delegation_ack_filler=False)

    assert rpc.calls == []
    await session.stop()


def test_response_cancel_uses_provider_data_channel() -> None:
    peer = FakePeer()
    session = RealtimeSession(SdpFirstRpc(), "thread-1", peer=peer, timeout=1)
    session._started = True

    session.request_response_cancel()

    assert peer.sent_data_events == ['{"type":"response.cancel"}']


def test_response_cancel_requires_a_started_session() -> None:
    session = RealtimeSession(SdpFirstRpc(), "thread-1", peer=FakePeer(), timeout=1)

    with pytest.raises(ProtocolError, match="realtime session has not started"):
        session.request_response_cancel()


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


@pytest.mark.asyncio
async def test_unmonitored_input_drain_preserves_app_server_events() -> None:
    rpc = SdpFirstRpc()
    peer = BlockingDrainPeer()
    session = RealtimeSession(rpc, "thread-1", peer=peer, timeout=1)
    drain = asyncio.create_task(
        session.wait_input_drained(monitor_app_server_exit=False)
    )
    await peer.drain_started.wait()
    event = {
        "method": "thread/realtime/transcript/delta",
        "params": {
            "threadId": "thread-1",
            "role": "user",
            "delta": "Turn on the lights.",
        },
    }
    await rpc.subscription.events.put(event)

    await asyncio.sleep(0)
    assert rpc.subscription.events.qsize() == 1
    assert not session._backlog

    peer.release_drain.set()
    await drain
    assert await asyncio.wait_for(session.next_event(), timeout=0.2) == event
    await session.stop()


@pytest.mark.asyncio
async def test_stop_bounds_hung_app_server_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(realtime_module, "_STOP_TIMEOUT_SECONDS", 0.02)
    rpc = ControlledStopRpc(block_stop=True)
    peer = ControlledClosePeer()
    session = RealtimeSession(rpc, "thread-1", peer=peer, timeout=1)
    session._started = True

    await asyncio.wait_for(session.stop(), timeout=0.5)

    assert rpc.stop_calls == 1
    assert rpc.stop_timeouts == [pytest.approx(0.02)]
    assert rpc.stop_cancelled is True
    assert peer.closed is True
    assert peer.close_cancelled is False
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
async def test_stop_bounds_hung_peer_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(realtime_module, "_STOP_TIMEOUT_SECONDS", 0.02)
    rpc = ControlledStopRpc()
    peer = ControlledClosePeer(block_close=True)
    session = RealtimeSession(rpc, "thread-1", peer=peer, timeout=1)
    session._started = True

    await asyncio.wait_for(session.stop(), timeout=0.5)

    assert rpc.stop_calls == 1
    assert rpc.stop_cancelled is False
    assert peer.close_calls == 1
    assert peer.close_cancelled is True
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
async def test_stop_bounds_hung_rpc_and_peer_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(realtime_module, "_STOP_TIMEOUT_SECONDS", 0.02)
    rpc = ControlledStopRpc(block_stop=True)
    peer = ControlledClosePeer(block_close=True)
    session = RealtimeSession(rpc, "thread-1", peer=peer, timeout=1)
    session._started = True

    await asyncio.wait_for(session.stop(), timeout=0.5)

    assert rpc.stop_cancelled is True
    assert peer.close_cancelled is True
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_error", "close_error"),
    [
        (RuntimeError("stop failed"), None),
        (None, RuntimeError("close failed")),
        (RuntimeError("stop failed"), RuntimeError("close failed")),
    ],
)
async def test_stop_contains_cleanup_exceptions_and_closes_subscription(
    stop_error: Exception | None,
    close_error: Exception | None,
) -> None:
    rpc = ControlledStopRpc(stop_error=stop_error)
    peer = ControlledClosePeer(close_error=close_error)
    session = RealtimeSession(rpc, "thread-1", peer=peer, timeout=1)
    session._started = True

    await session.stop()

    assert rpc.stop_calls == 1
    assert peer.close_calls == 1
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
async def test_stop_is_idempotent_for_concurrent_and_repeat_callers() -> None:
    rpc = ControlledStopRpc(block_stop=True)
    peer = ControlledClosePeer(block_close=True)
    session = RealtimeSession(rpc, "thread-1", peer=peer, timeout=1)
    session._started = True

    first = asyncio.create_task(session.stop())
    second = asyncio.create_task(session.stop())
    await asyncio.wait_for(rpc.stop_started.wait(), timeout=0.2)
    await asyncio.wait_for(peer.close_started.wait(), timeout=0.2)
    rpc.release_stop.set()
    peer.release_close.set()

    await asyncio.gather(first, second)
    await session.stop()

    assert rpc.stop_calls == 1
    assert peer.close_calls == 1
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
async def test_cancelled_stop_caller_does_not_abandon_shared_cleanup() -> None:
    rpc = ControlledStopRpc(block_stop=True)
    peer = ControlledClosePeer(block_close=True)
    session = RealtimeSession(rpc, "thread-1", peer=peer, timeout=1)
    session._started = True
    caller = asyncio.create_task(session.stop())
    await asyncio.wait_for(rpc.stop_started.wait(), timeout=0.2)
    await asyncio.wait_for(peer.close_started.wait(), timeout=0.2)

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await session.stop()

    assert rpc.stop_calls == 1
    assert peer.close_calls == 1
    assert peer.close_cancelled is True
    assert rpc.stop_cancelled is True
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
async def test_strict_stop_confirmed_close_is_idempotent() -> None:
    rpc = ControlledStopRpc(emit_closed_on_stop=True)
    peer = ControlledClosePeer()
    session = RealtimeSession(rpc, "thread-1", peer=peer, timeout=1)
    session._started = True
    rpc.subscription.events.put_nowait(
        {
            "method": "thread/realtime/progress",
            "params": {"threadId": "thread-1", "sequence": "before-closed"},
        }
    )

    await session.stop_strict()
    await session.stop_strict()

    assert rpc.stop_calls == 1
    assert peer.close_calls == 1
    assert peer.closed is True
    assert rpc.subscription.closed is True
    assert rpc.subscription.events.empty()


@pytest.mark.asyncio
async def test_strict_stop_waiter_cancellation_rejoins_authoritative_stop() -> None:
    rpc = ControlledStopRpc(block_stop=True, emit_closed_on_stop=True)
    peer = ControlledClosePeer()
    session = RealtimeSession(rpc, "thread-1", peer=peer, timeout=1)
    session._started = True
    first = asyncio.create_task(session.stop_strict())
    await asyncio.wait_for(rpc.stop_started.wait(), timeout=0.2)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert rpc.stop_cancelled is False

    second = asyncio.create_task(session.stop_strict())
    rpc.release_stop.set()
    await second
    await session.stop()

    assert rpc.stop_calls == 1
    assert rpc.stop_cancelled is False
    assert peer.close_calls == 1
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
async def test_strict_stop_propagates_provider_error_without_retry() -> None:
    rpc = ControlledStopRpc(stop_error=RuntimeError("unknown stop outcome"))
    peer = ControlledClosePeer()
    session = RealtimeSession(rpc, "thread-1", peer=peer, timeout=1)
    session._started = True

    with pytest.raises(RuntimeError, match="unknown stop outcome"):
        await session.stop_strict()
    await session.stop()

    assert rpc.stop_calls == 1
    assert peer.close_calls == 1
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
async def test_strict_stop_provider_error_event_is_ambiguous() -> None:
    rpc = ControlledStopRpc()
    peer = ControlledClosePeer()
    session = RealtimeSession(rpc, "thread-1", peer=peer, timeout=1)
    session._started = True
    rpc.subscription.events.put_nowait(
        {
            "method": "thread/realtime/error",
            "params": {"threadId": "thread-1", "message": "stop failed"},
        }
    )

    with pytest.raises(ProtocolError, match="provider error during stop"):
        await session.stop_strict()

    assert rpc.stop_calls == 1
    assert peer.close_calls == 1
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
async def test_strict_stop_rpc_return_without_closed_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(realtime_module, "_STOP_TIMEOUT_SECONDS", 0.02)
    rpc = ControlledStopRpc()
    peer = ControlledClosePeer()
    session = RealtimeSession(rpc, "thread-1", peer=peer, timeout=1)
    session._started = True

    with pytest.raises(TimeoutError, match="realtime stop outcome is ambiguous"):
        await session.stop_strict()

    assert rpc.stop_calls == 1
    assert peer.close_calls == 1
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
async def test_strict_stop_hard_bounds_provider_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(realtime_module, "_STOP_TIMEOUT_SECONDS", 0.02)
    rpc = ControlledStopRpc(block_stop=True)
    peer = ControlledClosePeer()
    session = RealtimeSession(rpc, "thread-1", peer=peer, timeout=1)
    session._started = True

    with pytest.raises(TimeoutError, match="realtime stop outcome is ambiguous"):
        await asyncio.wait_for(session.stop_strict(), timeout=0.5)

    assert rpc.stop_calls == 1
    assert rpc.stop_cancelled is True
    assert peer.close_calls == 1
    assert rpc.subscription.closed is True


@pytest.mark.asyncio
async def test_strict_stop_hard_bounds_peer_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(realtime_module, "_STOP_TIMEOUT_SECONDS", 0.02)
    rpc = ControlledStopRpc(emit_closed_on_stop=True)
    peer = ControlledClosePeer(block_close=True)
    session = RealtimeSession(rpc, "thread-1", peer=peer, timeout=1)
    session._started = True

    with pytest.raises(TimeoutError, match="realtime stop outcome is ambiguous"):
        await asyncio.wait_for(session.stop_strict(), timeout=0.5)

    assert rpc.stop_calls == 1
    assert peer.close_calls == 1
    assert peer.close_cancelled is True
    assert rpc.subscription.closed is True
