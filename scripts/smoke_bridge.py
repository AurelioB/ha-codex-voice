"""Run a small opt-in text conversation against a live Codex Voice bridge."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any
from uuid import uuid4

from aiohttp import ClientSession, WSMsgType


async def run_smoke(
    url: str,
    token: str,
    messages: list[str],
    conversation_id: str,
) -> list[str]:
    """Send sequential turns under one conversation id and return final text."""
    headers = {"Authorization": f"Bearer {token}"}
    outputs: list[str] = []
    async with ClientSession(headers=headers) as session:
        for text in messages:
            chunks: list[str] = []
            async with session.ws_connect(f"{url.rstrip('/')}/v1/conversation") as ws:
                await ws.send_json(
                    {
                        "type": "start",
                        "conversation_id": conversation_id,
                        "text": text,
                        "model": "gpt-5.6-sol",
                        "instructions": "Answer briefly and do not use tools.",
                        "messages": [{"role": "user", "content": text}],
                        "tools": [],
                    }
                )
                async for message in ws:
                    if message.type is not WSMsgType.TEXT:
                        continue
                    event: dict[str, Any] = json.loads(message.data)
                    if event.get("type") == "delta":
                        chunks.append(str(event.get("delta", "")))
                    elif event.get("type") == "error":
                        raise RuntimeError(str(event.get("error", "bridge error")))
                    elif event.get("type") == "done":
                        break
            outputs.append("".join(chunks).strip())
    return outputs


def main() -> None:
    """Parse arguments and run the smoke test without printing credentials."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    parser.add_argument(
        "--conversation-id",
        default=f"codex-voice-smoke-{uuid4()}",
    )
    parser.add_argument(
        "--message",
        action="append",
        dest="messages",
        required=True,
    )
    args = parser.parse_args()
    token = os.environ.get("HA_CODEX_BRIDGE_TOKEN")
    if not token:
        parser.error("HA_CODEX_BRIDGE_TOKEN is required")
    print(
        json.dumps(
            asyncio.run(
                run_smoke(args.url, token, args.messages, args.conversation_id)
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
