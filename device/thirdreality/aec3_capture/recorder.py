"""SoundCard-compatible facade over the native hardware-loopback AEC3 ABI."""

from __future__ import annotations

import ctypes
import importlib
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self

_ABI_VERSION = 1
_SAMPLE_RATE = 16_000
_PERIOD_FRAMES = 160
_DEFAULT_LIBRARY = Path(
    "/data/conf/codex-python/aec3_capture/lib/libcodex_aec3_capture.so"
)
_MAX_NATIVE_ERROR_BYTES = 4_096

_STATUS_NAMES = {
    -1: "invalid argument",
    -2: "invalid state",
    -3: "ALSA failure",
    -4: "AEC3 processing failure",
    -5: "capture timeout",
    -6: "capture stopped",
    -7: "capture ring overflow",
    -8: "internal failure",
}


class CaptureConfigurationError(RuntimeError):
    """The opt-in AEC3 capture contract is missing or incompatible."""


class CaptureRuntimeError(RuntimeError):
    """The native capture path failed after it was selected."""


@dataclass(frozen=True, slots=True)
class CaptureSettings:
    """Validated native capture settings.

    The hardware defaults are intentionally explicit. They match the codec
    loopback DAI on the ThirdReality speaker; silently probing another layout
    could feed ordinary microphone channels to AEC3 as a render reference.
    """

    library_path: Path = _DEFAULT_LIBRARY
    alsa_device: str = "hw:0,4"
    channels: int = 4
    mic_channel: int = 0
    reference_channel_a: int = 2
    reference_channel_b: int = 3
    ring_frames: int = 4_096
    startup_timeout_ms: int = 1_000
    read_timeout_ms: int = 2_000

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> CaptureSettings | None:
        """Return enabled settings, or ``None`` when the flag is absent/off."""

        values = os.environ if environ is None else environ
        enabled = _parse_flag(values.get("CODEX_AEC3_CAPTURE", "0"))
        if not enabled:
            return None
        settings = cls(
            library_path=Path(values.get("CODEX_AEC3_LIBRARY", str(_DEFAULT_LIBRARY))),
            alsa_device=values.get("CODEX_AEC3_DEVICE", "hw:0,4"),
            channels=_parse_int(values, "CODEX_AEC3_CHANNELS", 4),
            mic_channel=_parse_int(values, "CODEX_AEC3_MIC_CHANNEL", 0),
            reference_channel_a=_parse_int(values, "CODEX_AEC3_REFERENCE_CHANNEL_A", 2),
            reference_channel_b=_parse_int(values, "CODEX_AEC3_REFERENCE_CHANNEL_B", 3),
            ring_frames=_parse_int(values, "CODEX_AEC3_RING_FRAMES", 4_096),
            startup_timeout_ms=_parse_int(
                values, "CODEX_AEC3_STARTUP_TIMEOUT_MS", 1_000
            ),
            read_timeout_ms=_parse_int(values, "CODEX_AEC3_READ_TIMEOUT_MS", 2_000),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        """Reject layouts that cannot satisfy the synchronized AEC3 contract."""

        if not self.library_path.is_absolute():
            raise CaptureConfigurationError("CODEX_AEC3_LIBRARY must be absolute")
        if not self.alsa_device:
            raise CaptureConfigurationError("CODEX_AEC3_DEVICE must not be empty")
        if not 1 <= self.channels <= 32:
            raise CaptureConfigurationError("AEC3 channel count must be in 1..32")
        if not 0 <= self.mic_channel < self.channels:
            raise CaptureConfigurationError("AEC3 microphone channel is out of range")
        if not 0 <= self.reference_channel_a < self.channels:
            raise CaptureConfigurationError("AEC3 reference channel A is out of range")
        if self.reference_channel_b != -1 and not (
            0 <= self.reference_channel_b < self.channels
        ):
            raise CaptureConfigurationError("AEC3 reference channel B is out of range")
        reference_channels = {
            self.reference_channel_a,
            *(() if self.reference_channel_b == -1 else (self.reference_channel_b,)),
        }
        if self.mic_channel in reference_channels:
            raise CaptureConfigurationError(
                "AEC3 microphone and reference channels must be distinct"
            )
        if len(reference_channels) != (1 if self.reference_channel_b == -1 else 2):
            raise CaptureConfigurationError("AEC3 reference channels must be distinct")
        if self.ring_frames < _PERIOD_FRAMES * 2:
            raise CaptureConfigurationError(
                "AEC3 ring must hold at least two 10 ms periods"
            )
        if self.ring_frames > _SAMPLE_RATE * 2:
            raise CaptureConfigurationError("AEC3 ring must not exceed two seconds")
        if not 100 <= self.startup_timeout_ms <= 10_000:
            raise CaptureConfigurationError(
                "AEC3 startup timeout must be in 100..10000 ms"
            )
        if not 100 <= self.read_timeout_ms <= 10_000:
            raise CaptureConfigurationError(
                "AEC3 read timeout must be in 100..10000 ms"
            )


@dataclass(frozen=True, slots=True)
class CaptureStats:
    """Content-free native capture counters."""

    captured_frames: int
    delivered_frames: int
    dropped_frames: int
    recoveries: int
    short_reads: int
    processing_failures: int
    resets: int


class _CConfig(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("alsa_device", ctypes.c_char_p),
        ("sample_rate", ctypes.c_uint32),
        ("channels", ctypes.c_uint32),
        ("mic_channel", ctypes.c_uint32),
        ("reference_channel_a", ctypes.c_int32),
        ("reference_channel_b", ctypes.c_int32),
        ("period_frames", ctypes.c_uint32),
        ("ring_frames", ctypes.c_uint32),
        ("startup_timeout_ms", ctypes.c_uint32),
    ]


class _CStats(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("captured_frames", ctypes.c_uint64),
        ("delivered_frames", ctypes.c_uint64),
        ("dropped_frames", ctypes.c_uint64),
        ("recoveries", ctypes.c_uint64),
        ("short_reads", ctypes.c_uint64),
        ("processing_failures", ctypes.c_uint64),
        ("resets", ctypes.c_uint64),
    ]


class _CaptureStream(Protocol):
    def start(self) -> None: ...

    def read_into(
        self,
        output: ctypes.Array[ctypes.c_int16],
        frame_offset: int,
        frames: int,
        timeout_ms: int,
    ) -> int: ...

    def stats(self) -> CaptureStats: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


class _NativeLibrary:
    def __init__(self, path: Path) -> None:
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise CaptureConfigurationError(
                f"AEC3 native library is unavailable: {path}"
            ) from exc
        if not resolved.is_file():
            raise CaptureConfigurationError(
                f"AEC3 native library is not a regular file: {resolved}"
            )
        mode = getattr(os, "RTLD_LOCAL", 0) | getattr(os, "RTLD_NOW", 0)
        try:
            library = ctypes.CDLL(str(resolved), mode=mode)
        except OSError as exc:
            raise CaptureConfigurationError(
                f"AEC3 native library could not be loaded: {resolved}: {exc}"
            ) from exc
        self._library = library
        self._bind()
        abi = int(library.codex_aec3_abi_version())
        if abi != _ABI_VERSION:
            raise CaptureConfigurationError(
                f"AEC3 ABI mismatch: expected {_ABI_VERSION}, found {abi}"
            )

    def _bind(self) -> None:
        library = self._library
        library.codex_aec3_abi_version.argtypes = []
        library.codex_aec3_abi_version.restype = ctypes.c_uint32
        library.codex_aec3_default_config.argtypes = [ctypes.POINTER(_CConfig)]
        library.codex_aec3_default_config.restype = None
        library.codex_aec3_create.argtypes = [ctypes.POINTER(_CConfig)]
        library.codex_aec3_create.restype = ctypes.c_void_p
        library.codex_aec3_start.argtypes = [ctypes.c_void_p]
        library.codex_aec3_start.restype = ctypes.c_int32
        library.codex_aec3_read.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        library.codex_aec3_read.restype = ctypes.c_int32
        library.codex_aec3_get_stats.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_CStats),
        ]
        library.codex_aec3_get_stats.restype = ctypes.c_int32
        library.codex_aec3_copy_last_error.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        library.codex_aec3_copy_last_error.restype = ctypes.c_size_t
        library.codex_aec3_stop.argtypes = [ctypes.c_void_p]
        library.codex_aec3_stop.restype = None
        library.codex_aec3_destroy.argtypes = [ctypes.c_void_p]
        library.codex_aec3_destroy.restype = None

    def open(self, settings: CaptureSettings) -> _CaptureStream:
        return _NativeStream(self._library, settings)


class _NativeStream:
    def __init__(self, library: Any, settings: CaptureSettings) -> None:
        config = _CConfig()
        library.codex_aec3_default_config(ctypes.byref(config))
        device = settings.alsa_device.encode("utf-8", "strict")
        config.alsa_device = device
        config.channels = settings.channels
        config.mic_channel = settings.mic_channel
        config.reference_channel_a = settings.reference_channel_a
        config.reference_channel_b = settings.reference_channel_b
        config.ring_frames = settings.ring_frames
        config.startup_timeout_ms = settings.startup_timeout_ms
        handle = library.codex_aec3_create(ctypes.byref(config))
        if not handle:
            raise CaptureConfigurationError(
                "AEC3 native capture rejected its hardware configuration"
            )
        self._library = library
        self._handle = handle
        self._started = False

    def _error(self, status: int) -> CaptureRuntimeError:
        handle = self._handle
        required = int(self._library.codex_aec3_copy_last_error(handle, None, 0))
        size = max(1, min(required, _MAX_NATIVE_ERROR_BYTES))
        buffer = ctypes.create_string_buffer(size)
        self._library.codex_aec3_copy_last_error(handle, buffer, size)
        detail = buffer.value.decode("utf-8", "replace")
        label = _STATUS_NAMES.get(status, f"native status {status}")
        return CaptureRuntimeError(f"AEC3 {label}: {detail or 'no detail'}")

    def start(self) -> None:
        status = int(self._library.codex_aec3_start(self._handle))
        if status != 0:
            raise self._error(status)
        self._started = True

    def read_into(
        self,
        output: ctypes.Array[ctypes.c_int16],
        frame_offset: int,
        frames: int,
        timeout_ms: int,
    ) -> int:
        pointer = ctypes.cast(
            ctypes.byref(output, frame_offset * ctypes.sizeof(ctypes.c_int16)),
            ctypes.POINTER(ctypes.c_int16),
        )
        status = int(
            self._library.codex_aec3_read(self._handle, pointer, frames, timeout_ms)
        )
        if status < 0:
            raise self._error(status)
        if status == 0 or status > frames:
            raise CaptureRuntimeError(
                f"AEC3 native read returned invalid frame count {status}"
            )
        return status

    def stats(self) -> CaptureStats:
        stats = _CStats(
            struct_size=ctypes.sizeof(_CStats),
            abi_version=_ABI_VERSION,
        )
        status = int(
            self._library.codex_aec3_get_stats(self._handle, ctypes.byref(stats))
        )
        if status != 0:
            raise self._error(status)
        return CaptureStats(
            captured_frames=int(stats.captured_frames),
            delivered_frames=int(stats.delivered_frames),
            dropped_frames=int(stats.dropped_frames),
            recoveries=int(stats.recoveries),
            short_reads=int(stats.short_reads),
            processing_failures=int(stats.processing_failures),
            resets=int(stats.resets),
        )

    def stop(self) -> None:
        if self._handle and self._started:
            self._library.codex_aec3_stop(self._handle)
            self._started = False

    def close(self) -> None:
        if not self._handle:
            return
        self.stop()
        self._library.codex_aec3_destroy(self._handle)
        self._handle = None


class Aec3Recorder:
    """Context-managed recorder matching the subset used by firmware v1.1.7."""

    def __init__(
        self,
        settings: CaptureSettings,
        stream_factory: Callable[[CaptureSettings], _CaptureStream],
        *,
        blocksize: int,
    ) -> None:
        """Create a recorder; native capture starts on context entry."""

        self._settings = settings
        self._stream_factory = stream_factory
        self._blocksize = blocksize
        self._stream: _CaptureStream | None = None

    def __enter__(self) -> Self:
        """Open the synchronized hardware capture stream."""

        if self._stream is not None:
            raise CaptureRuntimeError("AEC3 recorder cannot be entered twice")
        stream = self._stream_factory(self._settings)
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        self._stream = stream
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop and release native capture regardless of body outcome."""

        del exc_type, exc_value, traceback
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()

    def record(self, numframes: int | None = None) -> Any:
        """Return exact float32 mono frames, without padding or silent fallback."""

        stream = self._stream
        if stream is None:
            raise CaptureRuntimeError("AEC3 recorder is not open")
        frames = self._blocksize if numframes is None else numframes
        if not isinstance(frames, int) or frames <= 0:
            raise ValueError("record frame count must be a positive integer")
        if frames > 1_000_000:
            raise ValueError("record frame count exceeds the bounded allocation")

        buffer_type = ctypes.c_int16 * frames
        buffer = buffer_type()
        offset = 0
        deadline = time.monotonic() + self._settings.read_timeout_ms / 1_000.0
        while offset < frames:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise CaptureRuntimeError(
                    f"AEC3 did not deliver an exact {frames}-frame recorder block"
                )
            timeout_ms = max(1, int(remaining_seconds * 1_000.0))
            offset += stream.read_into(
                buffer,
                offset,
                frames - offset,
                timeout_ms,
            )

        # NumPy is already a firmware dependency. Import lazily so disabled
        # configuration and host-side config validation do not require it.
        np = importlib.import_module("numpy")

        pcm = np.ctypeslib.as_array(buffer).astype(np.float32)
        pcm *= 1.0 / 32_768.0
        return pcm.reshape((frames, 1))

    @property
    def stats(self) -> CaptureStats:
        """Return native health counters without exposing audio content."""

        stream = self._stream
        if stream is None:
            raise CaptureRuntimeError("AEC3 recorder is not open")
        return stream.stats()


class Aec3Microphone:
    """Minimal SoundCard microphone replacement for the vendor main loop."""

    name = "codex-aec3-hardware-loopback"

    def __init__(
        self,
        settings: CaptureSettings,
        library: Any,
    ) -> None:
        """Bind validated settings to one already-verified native library."""

        self._settings = settings
        self._library = library

    def recorder(
        self,
        *,
        samplerate: int,
        channels: int,
        blocksize: int | None = None,
        **unsupported: Any,
    ) -> Aec3Recorder:
        """Return the exact recorder subset used by firmware v1.1.7."""

        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise CaptureConfigurationError(
                f"unsupported AEC3 recorder options: {names}"
            )
        if samplerate != _SAMPLE_RATE or channels != 1:
            raise CaptureConfigurationError(
                "AEC3 recorder requires 16000 Hz mono output"
            )
        if blocksize is None or blocksize <= 0:
            raise CaptureConfigurationError(
                "AEC3 recorder requires an explicit positive blocksize"
            )
        return Aec3Recorder(
            self._settings,
            self._library.open,
            blocksize=blocksize,
        )


@dataclass(slots=True)
class SoundcardPatch:
    """A reversible patch handle, primarily for startup checks and tests."""

    module: Any
    original_default_microphone: Callable[[], Any]
    replacement: Callable[[], Aec3Microphone]
    microphone: Aec3Microphone

    def uninstall(self) -> None:
        """Restore the original default microphone when still installed."""

        if self.module.default_microphone is self.replacement:
            self.module.default_microphone = self.original_default_microphone


def install_from_environment(
    *,
    environ: Mapping[str, str] | None = None,
    soundcard_module: Any | None = None,
    library_factory: Callable[[Path], Any] = _NativeLibrary,
) -> SoundcardPatch | None:
    """Install the recorder facade only when ``CODEX_AEC3_CAPTURE=1``.

    Loading and ABI validation happen before SoundCard is patched. An enabled
    but broken native path raises and therefore cannot silently expose raw or
    PulseAudio-processed microphone audio.
    """

    settings = CaptureSettings.from_environment(environ)
    if settings is None:
        return None
    if soundcard_module is None:
        soundcard_module = importlib.import_module("soundcard")

    settings.validate()
    library = library_factory(settings.library_path)
    microphone = Aec3Microphone(settings, library)
    original = soundcard_module.default_microphone

    def replacement() -> Aec3Microphone:
        return microphone

    soundcard_module.default_microphone = replacement
    return SoundcardPatch(soundcard_module, original, replacement, microphone)


def _parse_flag(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    raise CaptureConfigurationError(
        "CODEX_AEC3_CAPTURE must be one of 0/1, false/true, no/yes, off/on"
    )


def _parse_int(values: Mapping[str, str], name: str, default: int) -> int:
    value = values.get(name)
    if value is None:
        return default
    try:
        return int(value, 10)
    except ValueError as exc:
        raise CaptureConfigurationError(f"{name} must be an integer") from exc
