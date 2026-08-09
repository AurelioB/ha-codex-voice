"""Verify handoff availability or measure it without logging content.

This opt-in live probe consumes ChatGPT subscription availability. It keeps
credentials, ticket values, audio, expected text, and observed transcripts out
of its output. Only timings, byte counts, and hashes of normalized canary text
are reported. A default release returns ``handoff_available: false`` because
ticket issuance is intentionally disabled.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import re
import secrets
import statistics
import time
import wave
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientResponse, ClientSession

from bridge.audio import streaming_wav_header

_LANGUAGE = "en-US"
_VOICE = "cove"
_CANARY_WORDS = (
    "amber",
    "cedar",
    "comet",
    "copper",
    "dawn",
    "ember",
    "forest",
    "garden",
    "harbor",
    "indigo",
    "lantern",
    "maple",
    "meadow",
    "opal",
    "orchid",
    "pebble",
    "river",
    "silver",
    "spruce",
    "velvet",
    "willow",
)


@dataclass(repr=False, slots=True)
class _StreamMeasurement:
    """In-memory streamed audio and content-free timing markers."""

    pcm: bytes
    first_pcm_seconds: float
    total_seconds: float


async def _raise_for_status(response: ClientResponse, operation: str) -> None:
    if response.status < 400:
        return
    await response.read()
    raise RuntimeError(f"{operation} failed with HTTP {response.status}")


def _finite_wav_pcm(value: bytes) -> bytes:
    with wave.open(io.BytesIO(value), "rb") as source:
        if (
            source.getframerate() != 24_000
            or source.getnchannels() != 1
            or source.getsampwidth() != 2
        ):
            raise RuntimeError("finite synthesis returned an unexpected WAV shape")
        return source.readframes(source.getnframes())


async def _finite_synthesize(session: ClientSession, url: str, text: str) -> bytes:
    async with session.post(
        f"{url}/v1/synthesize",
        json={
            "text": text,
            "language": _LANGUAGE,
            "voice": _VOICE,
            "format": "wav",
        },
    ) as response:
        await _raise_for_status(response, "finite synthesis")
        return _finite_wav_pcm(await response.read())


async def _transcribe(
    session: ClientSession,
    url: str,
    pcm: bytes,
    *,
    request_handoff: bool,
) -> tuple[str, str | None, float]:
    payload: dict[str, Any] = {
        "audio": base64.b64encode(pcm).decode(),
        "format": "pcm",
        "metadata": {
            "language": _LANGUAGE,
            "codec": "pcm",
            "sample_rate": 24_000,
            "bit_rate": 16,
            "channels": 1,
        },
        "prompt": "Transcribe the spoken validation phrase exactly.",
    }
    if request_handoff:
        payload["speech_session_handoff"] = {
            "version": 1,
            "voice": _VOICE,
            "language": _LANGUAGE,
        }

    started = time.monotonic()
    async with session.post(f"{url}/v1/transcribe", json=payload) as response:
        await _raise_for_status(response, "transcription")
        result = await response.json()
    elapsed = time.monotonic() - started
    transcript = result.get("text")
    if not isinstance(transcript, str) or not transcript.strip():
        raise RuntimeError("transcription returned no text")

    if not request_handoff:
        return transcript, None, elapsed
    handoff = result.get("speech_session_handoff")
    if not isinstance(handoff, dict):
        return transcript, None, elapsed
    if (
        handoff.get("version") != 1
        or handoff.get("voice") != _VOICE
        or handoff.get("language") != _LANGUAGE
    ):
        raise RuntimeError("transcription returned incompatible handoff metadata")
    token = handoff.get("token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("transcription returned no handoff ticket")
    return transcript, token, elapsed


async def _stream_synthesize(
    session: ClientSession,
    url: str,
    text: str,
    *,
    token: str | None,
) -> _StreamMeasurement:
    payload = {
        "text": text,
        "language": _LANGUAGE,
        "voice": _VOICE,
        "format": "wav",
    }
    if token is not None:
        payload["speech_session_handoff_token"] = token

    expected_header = streaming_wav_header()
    started = time.monotonic()
    first_pcm_at: float | None = None
    value = bytearray()
    async with session.post(f"{url}/v1/synthesize/stream", json=payload) as response:
        await _raise_for_status(response, "streaming synthesis")
        async for chunk in response.content.iter_chunked(64 * 1024):
            if not chunk:
                continue
            value.extend(chunk)
            if first_pcm_at is None and len(value) > len(expected_header):
                first_pcm_at = time.monotonic()
    ended = time.monotonic()
    if first_pcm_at is None or not value.startswith(expected_header):
        raise RuntimeError("streaming synthesis returned no valid PCM WAV")
    pcm = bytes(value[len(expected_header) :])
    if not pcm or len(pcm) % 2:
        raise RuntimeError("streaming synthesis returned invalid PCM16 audio")
    return _StreamMeasurement(
        pcm=pcm,
        first_pcm_seconds=first_pcm_at - started,
        total_seconds=ended - started,
    )


async def _release(session: ClientSession, url: str, token: str) -> None:
    try:
        async with session.post(
            f"{url}/v1/speech-session/release",
            json={"speech_session_handoff_token": token},
        ) as response:
            await response.read()
    except Exception:  # noqa: BLE001 - cleanup must not hide the probe result
        return


def _new_canary() -> str:
    return " ".join(secrets.SystemRandom().sample(_CANARY_WORDS, 5))


def _normalized_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z]+", value.casefold()))


def _text_hash(value: str) -> str:
    return hashlib.sha256(_normalized_text(value).encode()).hexdigest()


async def run_probe(url: str, token: str, trials: int) -> dict[str, Any]:
    """Report disabled issuance or run alternating cold/warm timing trials."""
    base_url = url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    warm: list[_StreamMeasurement] = []
    cold: list[_StreamMeasurement] = []
    handoff_seconds: list[float] = []
    content: dict[str, Any] | None = None

    async with ClientSession(headers=headers) as session:
        seed_pcm = await _finite_synthesize(
            session,
            base_url,
            "The bridge handoff validation is ready.",
        )
        for trial in range(trials):
            canary = _new_canary()
            if trial % 2:
                cold_measurement = await _stream_synthesize(
                    session, base_url, canary, token=None
                )

            _, handoff_token, offer_seconds = await _transcribe(
                session,
                base_url,
                seed_pcm,
                request_handoff=True,
            )
            if handoff_token is None:
                return {
                    "handoff_available": False,
                    "trials_requested": trials,
                    "trials_completed": len(warm),
                    "last_handoff_transcription_seconds": offer_seconds,
                }
            try:
                warm_measurement = await _stream_synthesize(
                    session,
                    base_url,
                    canary,
                    token=handoff_token,
                )
            finally:
                await _release(session, base_url, handoff_token)

            if trial % 2 == 0:
                cold_measurement = await _stream_synthesize(
                    session, base_url, canary, token=None
                )

            warm.append(warm_measurement)
            cold.append(cold_measurement)
            handoff_seconds.append(offer_seconds)

            if content is None:
                observed, _, _ = await _transcribe(
                    session,
                    base_url,
                    warm_measurement.pcm,
                    request_handoff=False,
                )
                expected_hash = _text_hash(canary)
                observed_hash = _text_hash(observed)
                content = {
                    "normalized_match": secrets.compare_digest(
                        expected_hash, observed_hash
                    ),
                    "expected_sha256": expected_hash,
                    "observed_sha256": observed_hash,
                }

    warm_first = [item.first_pcm_seconds for item in warm]
    cold_first = [item.first_pcm_seconds for item in cold]
    deltas = [
        cold_value - warm_value
        for cold_value, warm_value in zip(cold_first, warm_first, strict=True)
    ]
    return {
        "handoff_available": True,
        "trials": trials,
        "content_validation": content,
        "median_seconds": {
            "handoff_transcription": statistics.median(handoff_seconds),
            "warm_first_pcm": statistics.median(warm_first),
            "cold_first_pcm": statistics.median(cold_first),
            "first_pcm_saved": statistics.median(deltas),
        },
        "runs": [
            {
                "warm_first_pcm_seconds": warm_item.first_pcm_seconds,
                "cold_first_pcm_seconds": cold_item.first_pcm_seconds,
                "first_pcm_saved_seconds": cold_item.first_pcm_seconds
                - warm_item.first_pcm_seconds,
                "warm_total_seconds": warm_item.total_seconds,
                "cold_total_seconds": cold_item.total_seconds,
                "warm_pcm_bytes": len(warm_item.pcm),
                "cold_pcm_bytes": len(cold_item.pcm),
            }
            for warm_item, cold_item in zip(warm, cold, strict=True)
        ],
    }


def main() -> None:
    """Parse safe live-probe options and print content-free measurements."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    parser.add_argument("--trials", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.trials <= 20:
        parser.error("--trials must be between 1 and 20")
    token = os.environ.get("HA_CODEX_BRIDGE_TOKEN")
    if not token:
        parser.error("HA_CODEX_BRIDGE_TOKEN is required")
    print(json.dumps(asyncio.run(run_probe(args.url, token, args.trials)), indent=2))


if __name__ == "__main__":
    main()
