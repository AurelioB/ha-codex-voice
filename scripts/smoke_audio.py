"""Run opt-in live STT and best-effort TTS checks against the bridge."""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import wave
from typing import Any

from aiohttp import ClientSession


async def run_smoke(url: str, token: str, phrase: str) -> dict[str, Any]:
    """Synthesize a phrase, validate its WAV, and transcribe it in memory."""
    headers = {"Authorization": f"Bearer {token}"}
    async with ClientSession(headers=headers) as session:
        async with session.post(
            f"{url.rstrip('/')}/v1/synthesize",
            json={
                "text": phrase,
                "language": "en-US",
                "voice": "cove",
                "format": "wav",
                "instructions": "Use a calm, concise delivery.",
            },
        ) as response:
            if response.status >= 400:
                raise RuntimeError(
                    f"synthesis failed ({response.status}): {await response.text()}"
                )
            synthesis_mode = response.headers.get("X-Codex-Synthesis-Mode")
            content_type = response.headers.get("Content-Type", "")
            wav_data = await response.read()

        with wave.open(io.BytesIO(wav_data), "rb") as audio:
            sample_rate = audio.getframerate()
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            frame_count = audio.getnframes()
            pcm = audio.readframes(frame_count)

        if sample_width != 2:
            raise RuntimeError(f"expected PCM16 WAV, received {sample_width * 8}-bit")

        async with session.post(
            f"{url.rstrip('/')}/v1/transcribe",
            json={
                "audio": base64.b64encode(pcm).decode(),
                "format": "pcm",
                "metadata": {
                    "language": "en-US",
                    "codec": "pcm",
                    "sample_rate": sample_rate,
                    "bit_rate": 16,
                    "channels": channels,
                },
                "prompt": "This is a short bridge validation phrase.",
            },
        ) as response:
            if response.status >= 400:
                raise RuntimeError(
                    f"transcription failed ({response.status}): {await response.text()}"
                )
            transcription = await response.json()

    return {
        "synthesis": {
            "content_type": content_type,
            "mode": synthesis_mode,
            "sample_rate": sample_rate,
            "channels": channels,
            "frames": frame_count,
        },
        "transcription": transcription,
    }


def main() -> None:
    """Parse arguments and run without printing credentials or audio."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    parser.add_argument(
        "--phrase",
        default="The blue lantern is ready.",
        help="Non-sensitive phrase used for the round-trip check.",
    )
    args = parser.parse_args()
    token = os.environ.get("HA_CODEX_BRIDGE_TOKEN")
    if not token:
        parser.error("HA_CODEX_BRIDGE_TOKEN is required")
    print(json.dumps(asyncio.run(run_smoke(args.url, token, args.phrase)), indent=2))


if __name__ == "__main__":
    main()
