"""Run a finite PCM WAV through a Wyoming speech-to-text server."""

from __future__ import annotations

import argparse
import asyncio
import json
import wave
from pathlib import Path
from time import monotonic

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient


def _result(
    transcript: str,
    duration: float,
    *,
    show_transcript: bool,
) -> dict[str, object]:
    """Build privacy-conscious smoke-test output."""
    result: dict[str, object] = {
        "duration_seconds": round(duration, 3),
        "transcript_received": bool(transcript.strip()),
    }
    if show_transcript:
        result["transcript"] = transcript
    return result


async def transcribe(
    path: Path,
    *,
    uri: str,
    language: str,
    timeout: float,
) -> tuple[str, float]:
    """Return the transcript and request duration for one uncompressed PCM WAV."""
    with wave.open(str(path), "rb") as source:
        if source.getcomptype() != "NONE" or source.getsampwidth() != 2:
            raise ValueError("WAV must contain uncompressed PCM16 audio")
        sample_rate = source.getframerate()
        channels = source.getnchannels()
        audio = source.readframes(source.getnframes())

    client = AsyncClient.from_uri(uri)
    async with asyncio.timeout(timeout):
        async with client:
            started = monotonic()
            await client.write_event(Transcribe(language=language).event())
            await client.write_event(
                AudioStart(rate=sample_rate, width=2, channels=channels).event()
            )

            bytes_per_chunk = max(1, sample_rate * 2 * channels // 10)
            for offset in range(0, len(audio), bytes_per_chunk):
                await client.write_event(
                    AudioChunk(
                        rate=sample_rate,
                        width=2,
                        channels=channels,
                        audio=audio[offset : offset + bytes_per_chunk],
                    ).event()
                )
            await client.write_event(AudioStop().event())

            while event := await client.read_event():
                if Transcript.is_type(event.type):
                    return Transcript.from_event(event).text, monotonic() - started
                if event.type == "error":
                    raise RuntimeError(str(event.data))

    raise RuntimeError("Wyoming server closed without returning a transcript")


def main() -> None:
    """Run one privacy-conscious local STT smoke test."""
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", type=Path)
    parser.add_argument("--uri", default="tcp://127.0.0.1:10300")
    parser.add_argument("--language", default="en")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--show-transcript",
        action="store_true",
        help="include recognized text in terminal output",
    )
    args = parser.parse_args()

    transcript, duration = asyncio.run(
        transcribe(
            args.wav,
            uri=args.uri,
            language=args.language,
            timeout=args.timeout,
        )
    )
    print(
        json.dumps(
            _result(
                transcript,
                duration,
                show_transcript=args.show_transcript,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
