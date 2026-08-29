"""Tiny stdio JSON-RPC peer used by bridge unit tests."""

from __future__ import annotations

import json
import os
import sys


def send(value: object) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> None:
    initialized = False
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            experimental = (
                message.get("params", {}).get("capabilities", {}).get("experimentalApi")
            )
            if experimental is not True:
                send(
                    {
                        "id": request_id,
                        "error": {"code": -32600, "message": "experimentalApi missing"},
                    }
                )
            else:
                initialized = True
                send({"id": request_id, "result": {"userAgent": "fake/0.146"}})
        elif method == "initialized":
            continue
        elif not initialized:
            send(
                {
                    "id": request_id,
                    "error": {"code": -32600, "message": "not initialized"},
                }
            )
        elif method == "thread/start":
            send({"id": request_id, "result": {"thread": {"id": "thread-1"}}})
            send({"method": "thread/started", "params": {"thread": {"id": "thread-1"}}})
        elif method == "account/read":
            send(
                {
                    "id": request_id,
                    "result": {
                        "account": {"type": "chatgpt", "planType": "plus"},
                        "requiresOpenaiAuth": True,
                    },
                }
            )
        elif method == "config/read":
            configured = (
                {"unexpected": {"command": "false"}}
                if os.environ.get("FAKE_MCP_SERVER")
                else {}
            )
            send(
                {
                    "id": request_id,
                    "result": {
                        "config": {},
                        "layers": [
                            {
                                "name": "commandLine",
                                "version": "1",
                                "config": {"mcp_servers": configured},
                            }
                        ],
                        "origins": {},
                    },
                }
            )
        elif method == "test/requestApproval":
            send({"id": request_id, "result": {}})
            send(
                {
                    "id": "approval-1",
                    "method": "item/commandExecution/requestApproval",
                    "params": {"threadId": "thread-1", "command": ["false"]},
                }
            )
        elif method == "test/requestTool":
            send({"id": request_id, "result": {}})
            send(
                {
                    "id": "rpc-tool-1",
                    "method": "item/tool/call",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "callId": "call-1",
                        "namespace": None,
                        "tool": "HassTurnOn",
                        "arguments": {"name": "Kitchen"},
                    },
                }
            )
        elif method == "test/requestCurrentTime":
            send({"id": request_id, "result": {}})
            send(
                {
                    "id": "current-time-1",
                    "method": "currentTime/read",
                    "params": {"threadId": "thread-1"},
                }
            )
        elif method == "test/largeResponse":
            send({"id": request_id, "result": {"payload": "x" * 100_000}})
        elif request_id == "approval-1" and "result" in message:
            send({"method": "fake/approvalResult", "params": message["result"]})
        elif request_id == "rpc-tool-1" and "result" in message:
            send({"method": "fake/toolResult", "params": message["result"]})
        elif request_id == "current-time-1" and "result" in message:
            send({"method": "fake/currentTimeResult", "params": message["result"]})
        elif request_id is not None:
            send({"id": request_id, "result": {}})


if __name__ == "__main__":
    main()
