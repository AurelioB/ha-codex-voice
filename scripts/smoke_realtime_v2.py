"""Run a content-free live smoke test of realtime device wire protocol v2."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import wave
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, WSMsgType


async def run_smoke(
    url: str,
    token: str,
    text: str,
    *,
    input_wav: Path | None = None,
    output_wav: Path | None = None,
) -> dict[str, Any]:
    """Send one text or paced-audio turn and validate epoch-gated output."""
    input_pcm: bytes | None = None
    input_sample_rate = 16_000
    if input_wav is not None:
        with wave.open(str(input_wav), "rb") as wav:
            if (
                wav.getcomptype() != "NONE"
                or wav.getnchannels() != 1
                or wav.getsampwidth() != 2
            ):
                raise ValueError("input WAV must be mono uncompressed PCM16")
            input_sample_rate = wav.getframerate()
            input_pcm = wav.readframes(wav.getnframes())
    headers = {"Authorization": f"Bearer {token}"}
    started_at = time.monotonic()
    handshake_at: float | None = None
    first_audio_at: float | None = None
    audio_bytes = 0
    audio = bytearray()
    active_epoch: int | None = None
    completed_epoch: int | None = None
    controls = 0

    async with (
        ClientSession(headers=headers) as session,
        session.ws_connect(f"{url.rstrip('/')}/v1/realtime") as websocket,
    ):
        await websocket.send_json(
            {
                "type": "start",
                "protocol_version": 2,
                "audio_transport": "binary",
                "input_sample_rate": input_sample_rate,
                "input_channels": 1,
            }
        )
        started = await websocket.receive_json(timeout=45)
        if started.get("type") != "started":
            raise RuntimeError("realtime v2 start failed")
        handshake_at = time.monotonic()
        capabilities = started.get("capabilities")
        if not isinstance(capabilities, dict) or capabilities != {
            "binary_pcm16": True,
            "local_flush": True,
            "remote_cancel": False,
            "same_session_interrupt_ack": True,
        }:
            raise RuntimeError("realtime v2 returned incompatible capabilities")

        if input_pcm is None:
            await websocket.send_json({"type": "text", "role": "user", "text": text})
        else:
            frame_bytes = input_sample_rate * 2 * 20 // 1_000
            audio_and_tail = input_pcm + bytes(input_sample_rate * 2)
            for offset in range(0, len(audio_and_tail), frame_bytes):
                frame = audio_and_tail[offset : offset + frame_bytes]
                await websocket.send_bytes(frame)
                await asyncio.sleep(len(frame) / (input_sample_rate * 2))
        deadline = asyncio.get_running_loop().time() + 90
        while completed_epoch is None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("realtime v2 output timed out")
            message = await websocket.receive(timeout=remaining)
            if message.type is WSMsgType.BINARY:
                if active_epoch is None:
                    raise RuntimeError("binary output arrived outside an epoch")
                chunk = bytes(message.data)
                if not chunk or len(chunk) % 2:
                    raise RuntimeError("realtime v2 returned invalid PCM16")
                if first_audio_at is None:
                    first_audio_at = time.monotonic()
                if output_wav is not None:
                    audio.extend(chunk)
                audio_bytes += len(chunk)
                continue
            if message.type is not WSMsgType.TEXT:
                raise RuntimeError("realtime v2 socket closed before output completed")
            event = json.loads(message.data)
            if event.get("type") == "error":
                raise RuntimeError("realtime v2 bridge returned an error")
            if event.get("type") != "control":
                continue
            controls += 1
            event_type = event.get("event_type")
            if event_type == "speaking.started":
                epoch = event.get("output_epoch")
                if not isinstance(epoch, int) or epoch < 1 or active_epoch is not None:
                    raise RuntimeError("realtime v2 returned an invalid output epoch")
                active_epoch = epoch
            elif event_type == "speaking.stopped":
                epoch = event.get("output_epoch")
                if active_epoch is None or epoch != active_epoch:
                    raise RuntimeError("realtime v2 stopped the wrong output epoch")
                completed_epoch = active_epoch
                active_epoch = None

        await websocket.send_json({"type": "stop"})

    if not audio_bytes or first_audio_at is None or handshake_at is None:
        raise RuntimeError("realtime v2 completed without audible PCM")
    if output_wav is not None:
        with output_wav.open("xb") as output, wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24_000)
            wav.writeframes(audio)
    return {
        "protocol_version": started.get("protocol_version"),
        "output_sample_rate": started.get("output_sample_rate"),
        "output_channels": started.get("output_channels"),
        "audio_bytes": audio_bytes,
        "controls": controls,
        "output_epoch": completed_epoch,
        "handshake_seconds": round(handshake_at - started_at, 3),
        "first_audio_seconds": round(first_audio_at - started_at, 3),
        "total_seconds": round(time.monotonic() - started_at, 3),
    }


def main() -> None:
    """Parse safe live-smoke options without printing credentials or content."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    parser.add_argument(
        "--text",
        default="Reply with a brief spoken confirmation that realtime is ready.",
    )
    parser.add_argument("--output-wav", type=Path)
    parser.add_argument("--input-wav", type=Path)
    args = parser.parse_args()
    token = os.environ.get("HA_CODEX_REALTIME_DEVICE_TOKEN") or os.environ.get(
        "HA_CODEX_BRIDGE_TOKEN"
    )
    if not token:
        parser.error(
            "HA_CODEX_REALTIME_DEVICE_TOKEN or HA_CODEX_BRIDGE_TOKEN is required"
        )
    print(
        json.dumps(
            asyncio.run(
                run_smoke(
                    args.url,
                    token,
                    args.text,
                    input_wav=args.input_wav,
                    output_wav=args.output_wav,
                )
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
