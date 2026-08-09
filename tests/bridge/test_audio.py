from __future__ import annotations

import base64
import wave
from io import BytesIO

import pytest

from bridge.audio import (
    Pcm16Mono24KhzResampler,
    Pcm16MonoResampler,
    decode_base64_audio,
    pcm16_mono_24khz,
    streaming_wav_header,
    wav_bytes,
)
from bridge.errors import ProtocolError


def test_pcm16_downmixes_and_resamples() -> None:
    # Two stereo frames at 12 kHz become four mono frames at 24 kHz.
    source = (
        (1_000).to_bytes(2, "little", signed=True)
        + (3_000).to_bytes(2, "little", signed=True)
        + (5_000).to_bytes(2, "little", signed=True)
        + (7_000).to_bytes(2, "little", signed=True)
    )
    output = pcm16_mono_24khz(source, 12_000, 2)
    samples = [
        int.from_bytes(output[index : index + 2], "little", signed=True)
        for index in range(0, len(output), 2)
    ]
    assert samples == [2_000, 4_000, 6_000, 6_000]


def test_wav_bytes_wrap_pcm16() -> None:
    payload = b"\x01\x00\x02\x00"
    with wave.open(BytesIO(wav_bytes(payload)), "rb") as audio:
        assert audio.getframerate() == 24_000
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.readframes(2) == payload


def test_streaming_wav_header_is_pcm16_and_uses_eof_length() -> None:
    pcm = b"\x01\x02" * 24_000
    with wave.open(BytesIO(streaming_wav_header() + pcm), "rb") as audio:
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.getframerate() == 24_000
        assert audio.getnframes() == 0xFFFFFFFF // 2
        assert audio.readframes(audio.getnframes()) == pcm


def test_base64_decoder_is_strict() -> None:
    assert decode_base64_audio(base64.b64encode(b"pcm").decode()) == b"pcm"
    with pytest.raises(ProtocolError):
        decode_base64_audio("not base64!")


def test_resampler_rejects_pathological_sample_rate() -> None:
    with pytest.raises(ProtocolError, match="sample_rate must be between"):
        pcm16_mono_24khz(b"\x00\x00", 1, 1)


@pytest.mark.parametrize("sample_rate", [16_000, 24_000, 48_000])
def test_streaming_resampler_matches_whole_clip_across_odd_chunks(
    sample_rate: int,
) -> None:
    source = b"".join(
        sample.to_bytes(2, "little", signed=True) for sample in range(-1_000, 1_001, 7)
    )
    expected = pcm16_mono_24khz(source, sample_rate, 1)
    resampler = Pcm16Mono24KhzResampler(sample_rate)

    chunks = [source[:14], source[14:84], source[84:222], source[222:]]
    actual = b"".join(resampler.feed(chunk) for chunk in chunks)
    actual += resampler.finish()

    assert actual == expected


def test_streaming_resampler_rejects_unaligned_or_late_input() -> None:
    resampler = Pcm16Mono24KhzResampler(16_000)
    with pytest.raises(ProtocolError, match="sample-aligned"):
        resampler.feed(b"\x00")
    assert resampler.finish() == b""
    with pytest.raises(ProtocolError, match="already finished"):
        resampler.feed(b"\x00\x00")


def test_streaming_resampler_converts_24khz_to_16khz() -> None:
    source = b"".join(
        sample.to_bytes(2, "little", signed=True) for sample in (0, 1_000, 2_000)
    )
    resampler = Pcm16MonoResampler(24_000, 16_000)

    output = resampler.feed(source[:2]) + resampler.feed(source[2:])
    output += resampler.finish()

    samples = [
        int.from_bytes(output[index : index + 2], "little", signed=True)
        for index in range(0, len(output), 2)
    ]
    assert samples == [0, 1_500]
