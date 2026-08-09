"""Probe subscription-backed Codex WebRTC input transcription.

This opt-in diagnostic reads one local PCM16 WAV, sends it through an isolated
Codex App Server runtime, and prints the returned transcript. Running it uses a
small amount of the signed-in ChatGPT subscription quota.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import wave
from pathlib import Path
from typing import Any

from bridge.audio import pcm16_mono_24khz
from bridge.config import BridgeConfig
from bridge.realtime import RealtimeSession
from bridge.service import (
    BridgeState,
    _dispose_thread,
    _prepare_transcription_audio,
    _transcription_prompt,
    _wait_for_user_transcript,
)


def _read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2 or source.getcomptype() != "NONE":
            raise ValueError("probe WAV must contain uncompressed PCM16 audio")
        pcm = source.readframes(source.getnframes())
        return pcm16_mono_24khz(
            pcm,
            sample_rate=source.getframerate(),
            channels=source.getnchannels(),
        )


async def run_probe(
    path: Path,
    *,
    version: str,
    model: str | None,
    language: str,
) -> dict[str, Any]:
    """Return one transcript without exposing OAuth material."""
    prepared = _prepare_transcription_audio(_read_wav(path))
    state = BridgeState(BridgeConfig(bearer_token="local-probe"))
    thread_id: str | None = None
    session: RealtimeSession | None = None
    try:
        await state.rpc.start()
        state.require_subscription_auth()
        thread_id = await state.start_thread(
            {},
            base_instructions=(
                "Act only as a speech recognition adapter. Never call tools, "
                "inspect files, or answer the user's speech."
            ),
        )
        session = RealtimeSession(
            state.rpc,
            thread_id,
            peer=state.peer_factory(),
            version=version,
            timeout=30,
        )
        await session.start(
            prompt=_transcription_prompt(language, "Known diagnostic phrase."),
            model=model,
            include_startup_context=False,
            client_managed_handoffs=True,
        )
        session.feed_audio(prepared.pcm)
        drain_task = asyncio.create_task(
            session.wait_input_drained(
                timeout=max(10.0, prepared.duration + 10.0),
                monitor_app_server_exit=False,
            )
        )
        try:
            transcript = await _wait_for_user_transcript(
                session,
                prepared.duration + 8.0,
                fragment_finalization_at=(
                    asyncio.get_running_loop().time() + prepared.duration
                ),
                input_drain_task=drain_task,
            )
        finally:
            if not drain_task.done():
                drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)
        return {
            "auth_mode": state.rpc.health().get("auth_mode"),
            "version": version,
            "model": model or "default",
            "duration_seconds": round(prepared.duration, 3),
            "transcript": transcript,
        }
    finally:
        try:
            if session is not None:
                await session.stop()
        finally:
            if thread_id is not None:
                await _dispose_thread(state.rpc, thread_id)
            await state.close()


def main() -> None:
    """Run one opt-in subscription transcription diagnostic."""
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", type=Path)
    parser.add_argument("--version", choices=("v1", "v3"), default="v3")
    parser.add_argument("--model")
    parser.add_argument("--language", default="en-US")
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                run_probe(
                    args.wav,
                    version=args.version,
                    model=args.model,
                    language=args.language,
                )
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
