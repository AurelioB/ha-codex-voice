from __future__ import annotations

import ctypes
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from device.thirdreality.aec3_capture import recorder


class _FakeStream:
    def __init__(
        self,
        samples: list[int] | None = None,
        *,
        maximum_read: int = 1_000,
        start_error: Exception | None = None,
    ) -> None:
        self.samples = list(samples or [])
        self.maximum_read = maximum_read
        self.start_error = start_error
        self.started = False
        self.stopped = False
        self.closed = False
        self.read_timeouts: list[int] = []

    def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    def read_into(
        self,
        output: ctypes.Array[ctypes.c_int16],
        frame_offset: int,
        frames: int,
        timeout_ms: int,
    ) -> int:
        self.read_timeouts.append(timeout_ms)
        count = min(frames, self.maximum_read, len(self.samples))
        if count == 0:
            raise recorder.CaptureRuntimeError("fake capture exhausted")
        for index in range(count):
            output[frame_offset + index] = self.samples.pop(0)
        return count

    def stats(self) -> recorder.CaptureStats:
        return recorder.CaptureStats(
            captured_frames=10,
            delivered_frames=9,
            dropped_frames=0,
            recoveries=1,
            short_reads=2,
            processing_failures=0,
            resets=3,
        )

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.stop()
        self.closed = True


class _FakeLibrary:
    def __init__(self, stream: _FakeStream) -> None:
        self.stream = stream
        self.settings: recorder.CaptureSettings | None = None

    def open(self, settings: recorder.CaptureSettings) -> _FakeStream:
        self.settings = settings
        return self.stream


def _settings(**overrides: Any) -> recorder.CaptureSettings:
    values = {
        "library_path": Path("/opt/codex-test/libcodex_aec3_capture.so"),
        "read_timeout_ms": 500,
    }
    values.update(overrides)
    return recorder.CaptureSettings(**values)


def test_capture_is_disabled_by_default() -> None:
    assert recorder.CaptureSettings.from_environment({}) is None
    assert (
        recorder.CaptureSettings.from_environment({"CODEX_AEC3_CAPTURE": "off"}) is None
    )


def test_enabled_environment_uses_exact_hardware_defaults() -> None:
    settings = recorder.CaptureSettings.from_environment(
        {
            "CODEX_AEC3_CAPTURE": "yes",
            "CODEX_AEC3_LIBRARY": "/opt/aec3/libcapture.so",
        }
    )

    assert settings is not None
    assert settings.library_path == Path("/opt/aec3/libcapture.so")
    assert settings.alsa_device == "hw:0,4"
    assert settings.channels == 4
    assert settings.mic_channel == 0
    assert (
        settings.reference_channel_a,
        settings.reference_channel_b,
    ) == (2, 3)


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"CODEX_AEC3_CAPTURE": "maybe"}, "must be one of"),
        (
            {
                "CODEX_AEC3_CAPTURE": "1",
                "CODEX_AEC3_LIBRARY": "relative.so",
            },
            "must be absolute",
        ),
        (
            {
                "CODEX_AEC3_CAPTURE": "1",
                "CODEX_AEC3_MIC_CHANNEL": "2",
            },
            "must be distinct",
        ),
        (
            {
                "CODEX_AEC3_CAPTURE": "1",
                "CODEX_AEC3_RING_FRAMES": "160",
            },
            "at least two",
        ),
    ],
)
def test_invalid_enabled_configuration_fails_closed(
    environment: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(recorder.CaptureConfigurationError, match=message):
        recorder.CaptureSettings.from_environment(environment)


def test_install_does_not_load_or_patch_when_disabled() -> None:
    original = object()
    soundcard = SimpleNamespace(default_microphone=original)
    loaded: list[Path] = []

    result = recorder.install_from_environment(
        environ={},
        soundcard_module=soundcard,
        library_factory=lambda path: loaded.append(path),
    )

    assert result is None
    assert soundcard.default_microphone is original
    assert loaded == []


def test_enabled_install_patches_and_can_be_reversed() -> None:
    original_microphone = object()

    def original() -> object:
        return original_microphone

    soundcard = SimpleNamespace(default_microphone=original)
    stream = _FakeStream([0])
    library = _FakeLibrary(stream)
    paths: list[Path] = []

    patch = recorder.install_from_environment(
        environ={
            "CODEX_AEC3_CAPTURE": "1",
            "CODEX_AEC3_LIBRARY": "/opt/aec3/libcapture.so",
        },
        soundcard_module=soundcard,
        library_factory=lambda path: paths.append(path) or library,
    )

    assert patch is not None
    assert paths == [Path("/opt/aec3/libcapture.so")]
    assert soundcard.default_microphone() is patch.microphone
    patch.uninstall()
    assert soundcard.default_microphone() is original_microphone


def test_recorder_aggregates_partial_native_reads_without_padding() -> None:
    stream = _FakeStream([-32768, -1, 0, 1, 32767], maximum_read=2)
    library = _FakeLibrary(stream)
    microphone = recorder.Aec3Microphone(_settings(), library)

    with microphone.recorder(
        samplerate=16_000,
        channels=1,
        blocksize=5,
    ) as capture:
        result = capture.record(5)
        assert capture.stats.resets == 3

    assert result.shape == (5, 1)
    assert result.dtype == np.float32
    np.testing.assert_allclose(
        result[:, 0],
        np.array([-1.0, -1 / 32768, 0, 1 / 32768, 32767 / 32768]),
    )
    assert len(stream.read_timeouts) == 3
    assert stream.started
    assert stream.stopped
    assert stream.closed


def test_recorder_closes_stream_when_native_start_fails() -> None:
    failure = recorder.CaptureRuntimeError("playback DMA is not running")
    stream = _FakeStream(start_error=failure)
    capture = recorder.Aec3Recorder(
        _settings(),
        lambda settings: stream,
        blocksize=1_024,
    )

    with pytest.raises(recorder.CaptureRuntimeError, match="playback DMA"):
        capture.__enter__()

    assert stream.closed


@pytest.mark.parametrize(
    ("samplerate", "channels"),
    [(48_000, 1), (16_000, 2)],
)
def test_microphone_rejects_non_vendor_output_format(
    samplerate: int,
    channels: int,
) -> None:
    microphone = recorder.Aec3Microphone(
        _settings(),
        _FakeLibrary(_FakeStream()),
    )

    with pytest.raises(recorder.CaptureConfigurationError, match="16000 Hz mono"):
        microphone.recorder(
            samplerate=samplerate,
            channels=channels,
            blocksize=1_024,
        )


def test_enabled_missing_library_does_not_patch_soundcard(tmp_path: Path) -> None:
    original = object()
    soundcard = SimpleNamespace(default_microphone=original)
    missing = tmp_path / "missing.so"

    with pytest.raises(recorder.CaptureConfigurationError, match="unavailable"):
        recorder.install_from_environment(
            environ={
                "CODEX_AEC3_CAPTURE": "1",
                "CODEX_AEC3_LIBRARY": str(missing),
            },
            soundcard_module=soundcard,
        )

    assert soundcard.default_microphone is original


def test_c_struct_layout_matches_aarch64_abi() -> None:
    assert ctypes.sizeof(recorder._CConfig) == 48
    assert ctypes.sizeof(recorder._CStats) == 64


def test_release_archive_contains_native_aec3_sources() -> None:
    root = Path(__file__).parents[2]
    workflow = (root / ".github/workflows/release.yml").read_text()

    assert "release/thirdreality-realtime/aec3_capture/cmake" in workflow
    assert "device/thirdreality/aec3_capture/build_aarch64.py" in workflow
    assert "device/thirdreality/aec3_capture/src/*" in workflow


def test_native_processor_conditions_capture_before_vendor_wake_and_realtime() -> None:
    root = Path(__file__).parents[2]
    source = (
        root / "device/thirdreality/aec3_capture/src/capture_engine.cpp"
    ).read_text()

    assert "constexpr float kFixedCaptureGainDb = 10.0F;" in source
    assert "constexpr float kMaximumOutputNoiseDbfs = -50.0F;" in source
    assert "processing_config.gain_controller2.enabled = true;" in source
    assert (
        "processing_config.gain_controller2.adaptive_digital.enabled = true;" in source
    )
    assert "processing_config.noise_suppression.enabled = true;" in source
    assert "NoiseSuppression::kModerate" in source
