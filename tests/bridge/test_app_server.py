from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from bridge.app_server import MAX_RETIRED_SERVER_RESPONSE_IDS, CodexAppServer
from bridge.errors import AppServerExited, ProtocolError

FAKE_SERVER = Path(__file__).with_name("fake_app_server.py")


@pytest.mark.asyncio
async def test_initializes_experimental_api_and_streams_notifications() -> None:
    rpc = CodexAppServer((sys.executable, str(FAKE_SERVER)), request_timeout=2)
    await rpc.start()
    subscription = rpc.subscribe()
    try:
        result = await rpc.call("thread/start", {})
        assert result["thread"]["id"] == "thread-1"
        event = await subscription.get(timeout=1)
        assert event["method"] == "thread/started"
        assert rpc.health()["running"] is True
    finally:
        subscription.close()
        await rpc.close()


@pytest.mark.asyncio
async def test_approvals_fail_closed_and_dynamic_tools_round_trip() -> None:
    rpc = CodexAppServer((sys.executable, str(FAKE_SERVER)), request_timeout=2)
    await rpc.start()
    subscription = rpc.subscribe()
    try:
        await rpc.call("test/requestApproval", {})
        approval = await subscription.get(timeout=1)
        assert approval == {
            "method": "fake/approvalResult",
            "params": {"decision": "decline"},
        }

        await rpc.call("test/requestTool", {})
        tool_call = await subscription.get(timeout=1)
        assert tool_call["method"] == "item/tool/call"
        assert tool_call["params"]["callId"] == "call-1"
        await rpc.respond_result(
            tool_call["id"],
            {
                "contentItems": [{"type": "inputText", "text": "done"}],
                "success": True,
            },
        )
        tool_result = await subscription.get(timeout=1)
        assert tool_result["method"] == "fake/toolResult"
        assert tool_result["params"]["success"] is True
    finally:
        subscription.close()
        await rpc.close()


@pytest.mark.asyncio
async def test_tool_fallback_remains_owned_until_result_write_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rpc = CodexAppServer((sys.executable, str(FAKE_SERVER)), tool_timeout=3600)
    subscription = rpc.subscribe()
    await rpc._handle_server_request(
        {
            "id": "provider-request",
            "method": "item/tool/call",
            "params": {},
        }
    )
    timer = rpc._server_request_timers["provider-request"]
    write_started = asyncio.Event()
    release_write = asyncio.Event()

    async def failing_write(value: object) -> None:
        del value
        write_started.set()
        await asyncio.wait_for(release_write.wait(), timeout=1)
        raise AppServerExited("test write failure")

    monkeypatch.setattr(rpc, "_write", failing_write)
    response = asyncio.create_task(
        rpc.respond_result("provider-request", {"success": True})
    )
    await asyncio.wait_for(write_started.wait(), timeout=1)

    assert rpc._server_request_timers["provider-request"] is timer
    assert not timer.cancelled()

    release_write.set()
    with pytest.raises(AppServerExited, match="test write failure"):
        await response
    assert rpc._server_request_timers["provider-request"] is timer
    assert not timer.cancelled()

    cancel_started = asyncio.Event()

    async def cancelled_write(value: object) -> None:
        del value
        cancel_started.set()
        await asyncio.wait_for(asyncio.Event().wait(), timeout=1)

    monkeypatch.setattr(rpc, "_write", cancelled_write)
    response = asyncio.create_task(
        rpc.respond_result("provider-request", {"success": True})
    )
    await asyncio.wait_for(cancel_started.wait(), timeout=1)
    response.cancel()
    with pytest.raises(asyncio.CancelledError):
        await response
    assert rpc._server_request_timers["provider-request"] is timer
    assert not timer.cancelled()

    writes: list[object] = []

    async def successful_write(value: object) -> None:
        writes.append(value)

    monkeypatch.setattr(rpc, "_write", successful_write)
    await rpc.respond_result("provider-request", {"success": True})
    await asyncio.sleep(0)

    assert writes == [{"id": "provider-request", "result": {"success": True}}]
    assert "provider-request" not in rpc._server_request_timers
    await asyncio.wait_for(asyncio.gather(timer, return_exceptions=True), timeout=1)
    assert timer.done()
    subscription.close()


@pytest.mark.asyncio
async def test_tool_timeout_and_normal_result_race_writes_only_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rpc = CodexAppServer((sys.executable, str(FAKE_SERVER)), tool_timeout=3600)
    subscription = rpc.subscribe()
    request_id = "provider-request"
    await rpc._handle_server_request(
        {
            "id": request_id,
            "method": "item/tool/call",
            "params": {},
        }
    )
    original_timer = rpc._server_request_timers.pop(request_id)
    original_timer.cancel()
    await asyncio.wait_for(
        asyncio.gather(original_timer, return_exceptions=True), timeout=1
    )

    fallback_write_started = asyncio.Event()
    release_fallback_write = asyncio.Event()
    writes: list[object] = []
    fallback_response = {
        "id": request_id,
        "result": {
            "contentItems": [
                {
                    "type": "inputText",
                    "text": (
                        '{"error":"home_assistant_tool_outcome_unknown",'
                        '"do_not_retry":true}'
                    ),
                }
            ],
            "success": False,
        },
    }

    async def record_write(value: object) -> None:
        if not fallback_write_started.is_set():
            fallback_write_started.set()
            await asyncio.wait_for(release_fallback_write.wait(), timeout=1)
        writes.append(value)

    monkeypatch.setattr(rpc, "_write", record_write)
    rpc.tool_timeout = 0
    state = rpc._server_response_states[request_id]
    fallback = asyncio.create_task(rpc._expire_server_request(request_id, state))
    state.timer = fallback
    rpc._server_request_timers[request_id] = fallback
    await asyncio.wait_for(fallback_write_started.wait(), timeout=1)

    normal_result = {
        "contentItems": [{"type": "inputText", "text": '{"success":true}'}],
        "success": True,
    }
    normal = asyncio.create_task(rpc.respond_result(request_id, normal_result))
    await asyncio.sleep(0)
    assert not normal.done()

    release_fallback_write.set()
    await asyncio.wait_for(asyncio.gather(fallback, normal), timeout=1)

    assert writes == [fallback_response]
    assert request_id not in rpc._server_request_timers
    assert request_id in rpc._retired_server_response_ids

    await rpc.respond_result(request_id, normal_result)
    assert writes == [fallback_response]
    subscription.close()


@pytest.mark.asyncio
async def test_retired_server_response_ids_are_bounded_without_evicting_active() -> (
    None
):
    rpc = CodexAppServer((sys.executable, str(FAKE_SERVER)), tool_timeout=3600)
    subscription = rpc.subscribe()
    active_request_id = "active-provider-request"
    await rpc._handle_server_request(
        {
            "id": active_request_id,
            "method": "item/tool/call",
            "params": {},
        }
    )
    active_state = rpc._server_response_states[active_request_id]
    old_timer = rpc._server_request_timers.pop(active_request_id)
    await asyncio.sleep(0)
    old_timer.cancel()
    replacement_timer = asyncio.create_task(asyncio.Event().wait())
    rpc._server_request_timers[active_request_id] = replacement_timer
    await asyncio.wait_for(asyncio.gather(old_timer, return_exceptions=True), timeout=1)

    for request_id in range(MAX_RETIRED_SERVER_RESPONSE_IDS + 1):
        rpc._retire_server_response_id(request_id)

    assert len(rpc._retired_server_response_ids) == MAX_RETIRED_SERVER_RESPONSE_IDS
    assert 0 not in rpc._retired_server_response_ids
    assert MAX_RETIRED_SERVER_RESPONSE_IDS in rpc._retired_server_response_ids
    assert rpc._server_response_states[active_request_id] is active_state
    assert rpc._server_request_timers[active_request_id] is replacement_timer

    rpc._server_request_timers.pop(active_request_id)
    replacement_timer.cancel()
    await asyncio.wait_for(
        asyncio.gather(replacement_timer, return_exceptions=True), timeout=1
    )
    subscription.close()


@pytest.mark.asyncio
async def test_startup_fails_closed_when_mcp_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_MCP_SERVER", "1")
    rpc = CodexAppServer((sys.executable, str(FAKE_SERVER)), request_timeout=2)

    with pytest.raises(ProtocolError, match="contains an MCP server"):
        await rpc.start()

    assert rpc.is_running is False
    await rpc.close()


@pytest.mark.asyncio
async def test_reads_legitimate_json_lines_above_asyncio_default() -> None:
    rpc = CodexAppServer((sys.executable, str(FAKE_SERVER)), request_timeout=2)
    await rpc.start()
    try:
        response = await rpc.call("test/largeResponse", {})
        assert len(response["payload"]) == 100_000
    finally:
        await rpc.close()
