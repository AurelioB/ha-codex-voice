"""Run an opt-in live full-duplex protocol check against the bridge."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from typing import Any

from aiohttp import ClientSession, WSMsgType


async def run_smoke(url: str, token: str, text: str) -> dict[str, Any]:
    """Send one text item and observe subscription-backed audio and transcript."""
    headers = {"Authorization": f"Bearer {token}"}
    audio_bytes = 0
    event_count = 0
    transcript = ""
    async with (
        ClientSession(headers=headers) as session,
        session.ws_connect(f"{url.rstrip('/')}/v1/realtime") as websocket,
    ):
        await websocket.send_json(
            {
                "type": "start",
                "conversation_id": "codex-voice-realtime-smoke",
                "include_startup_context": False,
                "client_managed_handoffs": False,
                "prompt": "Reply briefly as a spoken home assistant.",
                "voice": "cove",
            }
        )
        started = await websocket.receive_json(timeout=45)
        if started.get("type") != "started":
            raise RuntimeError(f"realtime start failed: {started}")
        await websocket.send_json({"type": "text", "role": "user", "text": text})

        deadline = asyncio.get_running_loop().time() + 90
        while not (audio_bytes and transcript):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("realtime audio/transcript timed out")
            message = await websocket.receive(timeout=remaining)
            if message.type is not WSMsgType.TEXT:
                if message.type in {
                    WSMsgType.CLOSE,
                    WSMsgType.CLOSED,
                    WSMsgType.ERROR,
                }:
                    raise RuntimeError("realtime websocket closed early")
                continue
            event: dict[str, Any] = json.loads(message.data)
            event_count += 1
            event_type = event.get("type")
            if event_type == "audio":
                audio_bytes += len(base64.b64decode(str(event.get("audio", ""))))
            elif event_type == "transcript_done" and event.get("role") != "user":
                transcript = str(event.get("text", "")).strip()
            elif event_type == "error":
                raise RuntimeError(str(event.get("error", "realtime bridge error")))

        await websocket.send_json({"type": "stop"})

    return {
        "sample_rate": started.get("sample_rate"),
        "channels": started.get("channels"),
        "audio_bytes": audio_bytes,
        "events": event_count,
        "transcript": transcript,
    }


def main() -> None:
    """Parse arguments and run without printing credentials or audio."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    parser.add_argument(
        "--text",
        default="Say that the realtime bridge is ready in five words or fewer.",
    )
    args = parser.parse_args()
    token = os.environ.get("HA_CODEX_BRIDGE_TOKEN")
    if not token:
        parser.error("HA_CODEX_BRIDGE_TOKEN is required")
    print(json.dumps(asyncio.run(run_smoke(args.url, token, args.text)), indent=2))


if __name__ == "__main__":
    main()
