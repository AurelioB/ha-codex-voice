from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bridge.app_server import CodexAppServer

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
