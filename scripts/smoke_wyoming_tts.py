"""Synthesize a fixed benign phrase with a Wyoming text-to-speech server."""

from __future__ import annotations

import argparse
import asyncio
import json
from time import monotonic

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.tts import Synthesize

_KNOWN_TEXT = "Hola. Esta es una prueba de voz local."
_CONNECT_RETRY_SECONDS = 0.1


def _result(
    *,
    duration: float,
    time_to_first_audio: float,
    rate: int,
    width: int,
    channels: int,
    chunk_count: int,
    audio_bytes: int,
) -> dict[str, object]:
    """Build text-free timing and PCM metadata."""
    bytes_per_second = rate * width * channels
    return {
        "duration_seconds": round(duration, 3),
        "time_to_first_audio_seconds": round(time_to_first_audio, 3),
        "audio": {
            "rate_hz": rate,
            "width_bytes": width,
            "channels": channels,
            "chunk_count": chunk_count,
            "bytes": audio_bytes,
            "duration_seconds": round(audio_bytes / bytes_per_second, 3),
        },
    }


async def synthesize(*, uri: str, timeout: float) -> dict[str, object]:
    """Synthesize known text and return text-free request and audio metadata."""
    client = AsyncClient.from_uri(uri)
    async with asyncio.timeout(timeout):
        async with client:
            started = monotonic()
            await client.write_event(Synthesize(text=_KNOWN_TEXT).event())

            audio_format: tuple[int, int, int] | None = None
            first_audio_at: float | None = None
            chunk_count = 0
            audio_bytes = 0

            while event := await client.read_event():
                if event.type == "error":
                    raise RuntimeError("Wyoming server returned an error")

                if AudioStart.is_type(event.type):
                    if audio_format is not None:
                        raise RuntimeError(
                            "Wyoming server returned duplicate audio start"
                        )
                    audio_start = AudioStart.from_event(event)
                    audio_format = (
                        audio_start.rate,
                        audio_start.width,
                        audio_start.channels,
                    )
                    if any(value <= 0 for value in audio_format):
                        raise RuntimeError(
                            "Wyoming server returned invalid audio format"
                        )
                    continue

                if AudioChunk.is_type(event.type):
                    if audio_format is None:
                        raise RuntimeError("Wyoming server returned audio before start")
                    chunk = AudioChunk.from_event(event)
                    chunk_format = (chunk.rate, chunk.width, chunk.channels)
                    if chunk_format != audio_format:
                        raise RuntimeError("Wyoming server changed audio format")
                    if not chunk.audio:
                        continue
                    if first_audio_at is None:
                        first_audio_at = monotonic()
                    chunk_count += 1
                    audio_bytes += len(chunk.audio)
                    continue

                if AudioStop.is_type(event.type):
                    if (
                        (audio_format is None)
                        or (first_audio_at is None)
                        or (audio_bytes <= 0)
                    ):
                        raise RuntimeError("Wyoming server returned no audio")
                    finished = monotonic()
                    rate, width, channels = audio_format
                    return _result(
                        duration=finished - started,
                        time_to_first_audio=first_audio_at - started,
                        rate=rate,
                        width=width,
                        channels=channels,
                        chunk_count=chunk_count,
                        audio_bytes=audio_bytes,
                    )

    raise RuntimeError("Wyoming server closed without returning complete audio")


async def synthesize_with_startup_retry(
    *,
    uri: str,
    timeout: float,
    retry_seconds: float = _CONNECT_RETRY_SECONDS,
) -> dict[str, object]:
    """Retry refused startup connections within one total timeout budget."""
    started = monotonic()
    while True:
        remaining = timeout - (monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("Wyoming server did not become ready")

        try:
            return await synthesize(uri=uri, timeout=remaining)
        except ConnectionRefusedError:
            remaining = timeout - (monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("Wyoming server did not become ready") from None
            await asyncio.sleep(min(retry_seconds, remaining))


def main() -> None:
    """Run one privacy-conscious local TTS smoke test."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="tcp://127.0.0.1:10200")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    result = asyncio.run(
        synthesize_with_startup_retry(uri=args.uri, timeout=args.timeout)
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
