"""Run a content-free bridge-PCM canary on a ThirdReality speaker."""

from __future__ import annotations

import argparse
import json
import time
import wave
from collections import Counter
from pathlib import Path

import soundcard
from realtime_client import (
    NATIVE_AEC3_CAPTURE,
    RealtimeSession,
    load_config,
    shutdown_all_sessions,
)
from realtime_client.config import BRIDGE_PCM_TRANSPORT

_FRAME_SAMPLES = 1_024
_FRAME_BYTES = _FRAME_SAMPLES * 2


def _pcm_frames(value: bytes):
    for offset in range(0, len(value), _FRAME_BYTES):
        frame = value[offset : offset + _FRAME_BYTES]
        yield frame + bytes(_FRAME_BYTES - len(frame))


def _read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as source:
        if (
            source.getcomptype() != "NONE"
            or source.getnchannels() != 1
            or source.getsampwidth() != 2
            or source.getframerate() != 16_000
        ):
            raise ValueError("canary WAV must be mono 16 kHz PCM16")
        return source.readframes(source.getnframes())


def run_probe(input_wav: Path) -> dict[str, object]:
    """Exercise the real device client while never forwarding ambient capture."""
    config = load_config()
    if config is None or config.media_transport != BRIDGE_PCM_TRANSPORT:
        raise RuntimeError("bridge_pcm configuration is not active")
    if not config.full_duplex or config.capture_backend != NATIVE_AEC3_CAPTURE:
        raise RuntimeError("native full-duplex capture is not active")

    input_pcm = _read_pcm(input_wav)
    microphone = soundcard.default_microphone()
    session: RealtimeSession | None = None
    submit_results: Counter[str] = Counter()
    ready_seconds: float | None = None
    first_output_seconds: float | None = None
    output_seen = False
    output_completed = False
    started_at = time.monotonic()
    try:
        with microphone.recorder(
            samplerate=16_000,
            channels=1,
            blocksize=_FRAME_SAMPLES,
        ) as recorder:
            # Keep the native capture/AEC path open for the same preflight used
            # by production, but discard all ambient samples in this probe.
            recorder.record(numframes=_FRAME_SAMPLES)
            session = RealtimeSession(config)
            session.start()
            startup_deadline = time.monotonic() + max(
                20.0,
                config.handshake_timeout_seconds + 10.0,
            )
            while not session.ready and not session.terminal:
                if time.monotonic() >= startup_deadline:
                    raise TimeoutError("device session did not become ready")
                recorder.record(numframes=_FRAME_SAMPLES)
            if not session.ready:
                raise RuntimeError("device session failed before ready")
            ready_seconds = time.monotonic() - started_at
            session.notify_live_capture_opened()

            # The input file must be a generated, non-user canary. Reading the
            # real microphone merely provides the media clock; those samples
            # are discarded and exact zeros are sent outside the fixed prompt.
            input_value = bytes(16_000) + input_pcm + bytes(16_000 * 2 * 2)
            for frame in _pcm_frames(input_value):
                recorder.record(numframes=_FRAME_SAMPLES)
                submit_results[session.submit_audio(frame).value] += 1

            response_deadline = time.monotonic() + 30.0
            output_quiet_at: float | None = None
            zero_frame = bytes(_FRAME_BYTES)
            while not session.terminal and time.monotonic() < response_deadline:
                recorder.record(numframes=_FRAME_SAMPLES)
                submit_results[session.submit_audio(zero_frame).value] += 1
                now = time.monotonic()
                if session.output_active:
                    if not output_seen:
                        output_seen = True
                        first_output_seconds = now - started_at
                    output_quiet_at = None
                elif output_seen:
                    if output_quiet_at is None:
                        output_quiet_at = now
                    elif now - output_quiet_at >= 1.0:
                        output_completed = True
                        break
    finally:
        if session is not None:
            session.stop()
            session.join(3.0)
        shutdown_all_sessions(3.0)

    result = {
        "transport": config.media_transport,
        "capture_backend": config.capture_backend,
        "ready_seconds": round(ready_seconds, 3) if ready_seconds else None,
        "first_output_seconds": (
            round(first_output_seconds, 3) if first_output_seconds else None
        ),
        "output_seen": output_seen,
        "output_completed": output_completed,
        "submit_results": dict(sorted(submit_results.items())),
        "terminal": session.terminal if session is not None else None,
        "state": session.state.value if session is not None else None,
    }
    if not output_seen or not output_completed:
        raise RuntimeError(f"bridge_pcm device probe failed: {result}")
    return result


def main() -> None:
    """Parse the fixed canary path and print content-free results."""
    parser = argparse.ArgumentParser()
    parser.add_argument("input_wav", type=Path)
    args = parser.parse_args()
    print(json.dumps(run_probe(args.input_wav), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
