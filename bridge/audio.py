"""Small, dependency-free PCM16 helpers."""

from __future__ import annotations

import base64
import binascii
import struct
import sys
import wave
from array import array
from io import BytesIO

from .errors import ProtocolError

PCM_SAMPLE_WIDTH = 2
REALTIME_SAMPLE_RATE = 24_000
MIN_PCM_SAMPLE_RATE = 8_000
MAX_PCM_SAMPLE_RATE = 192_000
MAX_PCM_DURATION_SECONDS = 300


def decode_base64_audio(value: object) -> bytes:
    """Decode a strict base64 payload and reject ambiguous input."""

    if not isinstance(value, str) or not value:
        raise ProtocolError("audio must be a non-empty base64 string")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProtocolError("audio is not valid base64") from exc


def encode_base64_audio(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def read_pcm16_payload(
    data: bytes,
    *,
    audio_format: str,
    codec: str | None,
    sample_rate: int,
    channels: int,
) -> tuple[bytes, int, int]:
    """Return little-endian PCM16 and its actual sample rate/channel count."""

    normalized_format = audio_format.lower().strip()
    normalized_codec = (codec or "pcm_s16le").lower().strip()
    if normalized_format in {"wav", "wave", "audio/wav"}:
        try:
            with wave.open(BytesIO(data), "rb") as source:
                if source.getsampwidth() != PCM_SAMPLE_WIDTH:
                    raise ProtocolError("WAV input must contain 16-bit PCM")
                if source.getcomptype() != "NONE":
                    raise ProtocolError("compressed WAV input is not supported")
                return (
                    source.readframes(source.getnframes()),
                    source.getframerate(),
                    source.getnchannels(),
                )
        except (wave.Error, EOFError) as exc:
            raise ProtocolError("invalid WAV input") from exc

    if normalized_format not in {"pcm", "raw", "audio/pcm", "pcm_s16le", "pcm16"}:
        raise ProtocolError(f"unsupported audio format: {audio_format}")
    if normalized_codec not in {
        "pcm",
        "pcm_s16le",
        "pcm16",
        "s16le",
        "audio/pcm",
        "linear16",
    }:
        raise ProtocolError(f"unsupported codec: {codec}")
    if sample_rate <= 0:
        raise ProtocolError("sample_rate must be positive")
    if channels not in {1, 2}:
        raise ProtocolError("only mono and stereo PCM are supported")
    if len(data) % (PCM_SAMPLE_WIDTH * channels):
        raise ProtocolError("PCM byte length is not aligned to complete frames")
    return data, sample_rate, channels


def pcm16_mono_24khz(data: bytes, sample_rate: int, channels: int) -> bytes:
    """Downmix and linearly resample PCM16 to the Realtime API wire format."""

    if not MIN_PCM_SAMPLE_RATE <= sample_rate <= MAX_PCM_SAMPLE_RATE:
        raise ProtocolError(
            f"sample_rate must be between {MIN_PCM_SAMPLE_RATE} and "
            f"{MAX_PCM_SAMPLE_RATE} Hz"
        )
    if channels not in {1, 2}:
        raise ProtocolError("only mono and stereo PCM are supported")
    if len(data) % (PCM_SAMPLE_WIDTH * channels):
        raise ProtocolError("PCM byte length is not aligned to complete frames")
    if not data:
        return b""

    input_frames = len(data) // (PCM_SAMPLE_WIDTH * channels)
    if input_frames > sample_rate * MAX_PCM_DURATION_SECONDS:
        raise ProtocolError(
            f"PCM input must not exceed {MAX_PCM_DURATION_SECONDS} seconds"
        )

    samples = array("h")
    samples.frombytes(data)
    if sys.byteorder != "little":
        samples.byteswap()

    if channels == 2:
        mono = array("h")
        mono.extend(
            (int(samples[index]) + int(samples[index + 1])) // 2
            for index in range(0, len(samples), 2)
        )
    else:
        mono = samples

    if sample_rate == REALTIME_SAMPLE_RATE:
        if sys.byteorder != "little":
            mono.byteswap()
        return mono.tobytes()

    output_length = max(1, round(len(mono) * REALTIME_SAMPLE_RATE / sample_rate))
    output = array("h")
    if len(mono) == 1:
        output.extend([mono[0]] * output_length)
    else:
        scale = sample_rate / REALTIME_SAMPLE_RATE
        last = len(mono) - 1
        for output_index in range(output_length):
            position = min(output_index * scale, last)
            left_index = int(position)
            right_index = min(left_index + 1, last)
            fraction = position - left_index
            value = round(
                mono[left_index] * (1.0 - fraction) + mono[right_index] * fraction
            )
            output.append(max(-32_768, min(32_767, value)))
    if sys.byteorder != "little":
        output.byteswap()
    return output.tobytes()


def silence_pcm16(duration_ms: int, sample_rate: int = REALTIME_SAMPLE_RATE) -> bytes:
    if duration_ms < 0:
        raise ProtocolError("silence duration must not be negative")
    return bytes(round(duration_ms * sample_rate / 1000) * PCM_SAMPLE_WIDTH)


def wav_bytes(
    pcm: bytes, sample_rate: int = REALTIME_SAMPLE_RATE, channels: int = 1
) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(PCM_SAMPLE_WIDTH)
        target.setframerate(sample_rate)
        target.writeframes(pcm)
    return output.getvalue()


def streaming_wav_header(
    sample_rate: int = REALTIME_SAMPLE_RATE, channels: int = 1
) -> bytes:
    """Return a PCM16 WAV header whose data length is terminated by stream EOF."""

    if sample_rate <= 0:
        raise ProtocolError("sample_rate must be positive")
    if not 0 < channels <= 0xFFFF:
        raise ProtocolError("channels must fit in an unsigned 16-bit integer")
    block_align = channels * PCM_SAMPLE_WIDTH
    byte_rate = sample_rate * block_align
    if byte_rate > 0xFFFFFFFF:
        raise ProtocolError("WAV byte rate exceeds the unsigned 32-bit limit")
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        0xFFFFFFFF,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        PCM_SAMPLE_WIDTH * 8,
        b"data",
        0xFFFFFFFF,
    )
