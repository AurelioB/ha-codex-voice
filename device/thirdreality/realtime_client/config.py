"""Secure configuration loading for the ThirdReality realtime client."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit

DEFAULT_CONFIG_PATH = Path("/data/conf/codex-realtime.json")
_MAX_CONFIG_BYTES = 16 * 1024
_MAX_PROMPT_CHARACTERS = 1_024
_RECORDER_FRAME_BYTES = 2_048
_NORMAL_WAKE_PHRASE = "okay nabu"
_VOICE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_PULSE_OBJECT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9._]{0,127}\Z")
DEFAULT_PULSE_AEC_SOURCE = "codex_echo_cancel_source"
DEFAULT_PULSE_AEC_SINK = "codex_echo_cancel_sink"
DEFAULT_PULSE_AEC_METHOD = "webrtc"
SUPPORTED_PULSE_AEC_METHODS = frozenset({"adrian", "speex", "webrtc"})
DEFAULT_AEC_TEST_VOLUME_PERCENT = 25
_PULSE_AEC_METHOD_ERROR = "pulse_aec_method must be 'adrian', 'speex', or 'webrtc'"
_ALLOWED_KEYS = frozenset(
    {
        "enabled",
        "url",
        "connect_address",
        "token",
        "wake_phrase",
        "voice",
        "prompt",
        "connect_timeout_seconds",
        "handshake_timeout_seconds",
        "io_timeout_seconds",
        "idle_timeout_seconds",
        "max_session_seconds",
        "ping_interval_seconds",
        "pong_timeout_seconds",
        "input_queue_bytes",
        "fallback_buffer_bytes",
        "output_queue_bytes",
        "max_message_bytes",
        "full_duplex",
        "pulse_aec_source",
        "pulse_aec_sink",
        "pulse_aec_method",
        "aec_test_volume_percent",
    }
)


class ConfigError(ValueError):
    """Raised when realtime configuration is unsafe or invalid."""


@dataclass(frozen=True, slots=True)
class RealtimeConfig:
    """Validated, bounded settings for one fresh realtime session."""

    url: str
    connect_address: str
    token: str = field(repr=False)
    wake_phrase: str
    connect_timeout_seconds: float
    handshake_timeout_seconds: float
    io_timeout_seconds: float
    idle_timeout_seconds: float
    max_session_seconds: float
    ping_interval_seconds: float
    pong_timeout_seconds: float
    input_queue_bytes: int
    fallback_buffer_bytes: int
    output_queue_bytes: int
    max_message_bytes: int
    full_duplex: bool
    voice: str | None = None
    prompt: str | None = field(default=None, repr=False)
    pulse_aec_source: str | None = None
    pulse_aec_sink: str | None = None
    pulse_aec_method: str | None = None
    aec_test_volume_percent: int = DEFAULT_AEC_TEST_VOLUME_PERCENT

    def __post_init__(self) -> None:
        """Keep direct construction as strict as the root-only JSON loader."""
        if not isinstance(self.full_duplex, bool):
            raise ConfigError("full_duplex must be a boolean")
        if (
            isinstance(self.aec_test_volume_percent, bool)
            or not isinstance(self.aec_test_volume_percent, int)
            or not 1 <= self.aec_test_volume_percent <= DEFAULT_AEC_TEST_VOLUME_PERCENT
        ):
            raise ConfigError(
                "aec_test_volume_percent must be an integer from 1 through 25"
            )
        for key, candidate in (
            ("pulse_aec_source", self.pulse_aec_source),
            ("pulse_aec_sink", self.pulse_aec_sink),
        ):
            if candidate is not None and (
                not isinstance(candidate, str)
                or not _PULSE_OBJECT_NAME.fullmatch(candidate)
            ):
                raise ConfigError(f"{key} must be a safe PulseAudio object name")
        if self.pulse_aec_method is not None and (
            not isinstance(self.pulse_aec_method, str)
            or self.pulse_aec_method not in SUPPORTED_PULSE_AEC_METHODS
        ):
            raise ConfigError(_PULSE_AEC_METHOD_ERROR)
        if self.full_duplex:
            if self.pulse_aec_source is None or self.pulse_aec_sink is None:
                raise ConfigError(
                    "full_duplex requires explicit pulse_aec_source and pulse_aec_sink"
                )
            if self.pulse_aec_method is None:
                object.__setattr__(self, "pulse_aec_method", DEFAULT_PULSE_AEC_METHOD)
        elif (
            self.pulse_aec_source is not None
            or self.pulse_aec_sink is not None
            or self.pulse_aec_method is not None
        ):
            raise ConfigError("PulseAudio AEC routing requires full_duplex")


def normalize_wake_phrase(value: str) -> str:
    """Normalize a detector phrase for an exact, case-insensitive comparison."""
    return " ".join(value.casefold().split())


def realtime_start_message(config: RealtimeConfig) -> dict[str, Any]:
    """Build the strict v2 start object, omitting unset session preferences."""
    value: dict[str, Any] = {
        "type": "start",
        "protocol_version": 2,
        "audio_transport": "binary",
        "input_sample_rate": 16_000,
        "input_channels": 1,
    }
    if config.voice is not None:
        value["voice"] = config.voice
    if config.prompt is not None:
        value["prompt"] = config.prompt
    return value


def load_config(
    path: Path = DEFAULT_CONFIG_PATH,
    *,
    expected_uid: int = 0,
) -> RealtimeConfig | None:
    """Load one root-only config, returning ``None`` when explicitly disabled."""
    raw = _read_secure_file(path, expected_uid=expected_uid)
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError("realtime config must be valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise ConfigError("realtime config must be a JSON object")
    unknown = set(decoded).difference(_ALLOWED_KEYS)
    if unknown:
        raise ConfigError("realtime config contains unsupported settings")

    enabled = decoded.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("enabled must be a boolean")
    if not enabled:
        return None

    parsed = _validated_url(_required_text(decoded, "url", maximum=2_048))
    connect_address = decoded.get("connect_address", parsed.hostname)
    if not isinstance(connect_address, str) or not connect_address:
        raise ConfigError("connect_address must be a numeric IP address")
    try:
        normalized_address = str(ipaddress.ip_address(connect_address))
    except ValueError as exc:
        raise ConfigError("connect_address must be a numeric IP address") from exc

    token = _required_text(decoded, "token", maximum=4_096)
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in token):
        raise ConfigError("token must not contain control characters")
    try:
        token.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ConfigError("token must contain only ASCII characters") from exc

    wake_phrase = normalize_wake_phrase(
        _required_text(decoded, "wake_phrase", maximum=64)
    )
    if not wake_phrase or wake_phrase == _NORMAL_WAKE_PHRASE:
        raise ConfigError("wake_phrase must be distinct from the normal wake phrase")
    if any(not character.isprintable() for character in wake_phrase):
        raise ConfigError("wake_phrase must contain only printable characters")

    input_queue_bytes = _bounded_int(
        decoded,
        "input_queue_bytes",
        # 2.048 seconds of 16 kHz mono PCM16. Startup buffering must remain
        # bounded tightly enough that a late handshake cannot make the whole
        # utterance feel stale.
        default=64 * 1024,
        minimum=32 * 1024,
        maximum=64 * 1024,
    )
    fallback_buffer_bytes = _bounded_int(
        decoded,
        "fallback_buffer_bytes",
        default=64 * 1024,
        minimum=16 * 1024,
        maximum=64 * 1024,
    )
    if fallback_buffer_bytes > input_queue_bytes:
        raise ConfigError("fallback_buffer_bytes must not exceed input_queue_bytes")
    max_message_bytes = _bounded_int(
        decoded,
        "max_message_bytes",
        default=64 * 1024,
        # The pinned recorder always submits 1,024 PCM16 samples per callback.
        # WebSocket framing overhead is outside this payload-size bound.
        minimum=_RECORDER_FRAME_BYTES,
        maximum=64 * 1024,
    )
    if max_message_bytes % 2:
        raise ConfigError("max_message_bytes must be PCM16 aligned")

    voice = _optional_voice(decoded)
    prompt = _optional_prompt(decoded)

    full_duplex = decoded.get("full_duplex", False)
    if not isinstance(full_duplex, bool):
        raise ConfigError("full_duplex must be a boolean")
    pulse_aec_source = _optional_pulse_name(decoded, "pulse_aec_source")
    pulse_aec_sink = _optional_pulse_name(decoded, "pulse_aec_sink")
    pulse_aec_method = _optional_pulse_aec_method(decoded)
    if full_duplex and (pulse_aec_source is None or pulse_aec_sink is None):
        raise ConfigError(
            "full_duplex requires explicit pulse_aec_source and pulse_aec_sink"
        )
    if full_duplex and pulse_aec_method is None:
        pulse_aec_method = DEFAULT_PULSE_AEC_METHOD
    if not full_duplex and (
        pulse_aec_source is not None
        or pulse_aec_sink is not None
        or pulse_aec_method is not None
    ):
        raise ConfigError("PulseAudio AEC routing requires full_duplex")

    config = RealtimeConfig(
        url=parsed.geturl(),
        connect_address=normalized_address,
        token=token,
        wake_phrase=wake_phrase,
        connect_timeout_seconds=_bounded_float(
            decoded,
            "connect_timeout_seconds",
            default=5.0,
            minimum=0.5,
            maximum=15.0,
        ),
        handshake_timeout_seconds=_bounded_float(
            decoded,
            "handshake_timeout_seconds",
            default=20.0,
            minimum=1.0,
            maximum=30.0,
        ),
        io_timeout_seconds=_bounded_float(
            decoded,
            "io_timeout_seconds",
            default=1.0,
            minimum=0.5,
            maximum=3.0,
        ),
        idle_timeout_seconds=_bounded_float(
            decoded,
            "idle_timeout_seconds",
            default=45.0,
            minimum=5.0,
            maximum=120.0,
        ),
        max_session_seconds=_bounded_float(
            decoded,
            "max_session_seconds",
            default=300.0,
            minimum=15.0,
            maximum=900.0,
        ),
        ping_interval_seconds=_bounded_float(
            decoded,
            "ping_interval_seconds",
            default=15.0,
            minimum=5.0,
            maximum=60.0,
        ),
        pong_timeout_seconds=_bounded_float(
            decoded,
            "pong_timeout_seconds",
            default=5.0,
            minimum=1.0,
            maximum=15.0,
        ),
        input_queue_bytes=input_queue_bytes,
        fallback_buffer_bytes=fallback_buffer_bytes,
        output_queue_bytes=_bounded_int(
            decoded,
            "output_queue_bytes",
            # 1.024 seconds of 24 kHz mono PCM16.
            default=48 * 1024,
            minimum=48 * 1024,
            maximum=192 * 1024,
        ),
        max_message_bytes=max_message_bytes,
        full_duplex=full_duplex,
        voice=voice,
        prompt=prompt,
        pulse_aec_source=pulse_aec_source,
        pulse_aec_sink=pulse_aec_sink,
        pulse_aec_method=pulse_aec_method,
        aec_test_volume_percent=_bounded_int(
            decoded,
            "aec_test_volume_percent",
            default=DEFAULT_AEC_TEST_VOLUME_PERCENT,
            minimum=1,
            maximum=DEFAULT_AEC_TEST_VOLUME_PERCENT,
        ),
    )
    _validate_start_message_size(config)
    return config


def _read_secure_file(path: Path, *, expected_uid: int) -> bytes:
    if not path.is_absolute():
        raise ConfigError("realtime config path must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ConfigError("realtime config could not be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ConfigError("realtime config must be a regular file")
        if metadata.st_uid != expected_uid:
            raise ConfigError("realtime config must be owned by root")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ConfigError(
                "realtime config must not be accessible by group or other"
            )
        if metadata.st_size > _MAX_CONFIG_BYTES:
            raise ConfigError("realtime config is too large")
        chunks: list[bytes] = []
        remaining = _MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 4_096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise ConfigError("realtime config is too large")
        return raw
    finally:
        os.close(descriptor)


def _validated_url(value: str) -> SplitResult:
    parsed = urlsplit(value)
    if parsed.scheme not in {"ws", "wss"}:
        raise ConfigError("url must use ws or wss")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError("url must not contain user information")
    if parsed.hostname is None:
        raise ConfigError("url must contain a host")
    if parsed.fragment:
        raise ConfigError("url must not contain a fragment")
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    if "\r" in target or "\n" in target:
        raise ConfigError("url contains an invalid request target")
    try:
        target.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ConfigError("url request target must be ASCII encoded") from exc
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ConfigError("url contains an invalid port") from exc
    return parsed


def _required_text(value: dict[str, Any], key: str, *, maximum: int) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate or len(candidate) > maximum:
        raise ConfigError(f"{key} must be a non-empty bounded string")
    return candidate


def _optional_voice(value: dict[str, Any]) -> str | None:
    if "voice" not in value:
        return None
    candidate = value.get("voice")
    if not isinstance(candidate, str) or not _VOICE_NAME.fullmatch(candidate):
        raise ConfigError("voice must be an ASCII name of 1 through 64 safe characters")
    return candidate.lower()


def _optional_prompt(value: dict[str, Any]) -> str | None:
    if "prompt" not in value:
        return None
    candidate = value.get("prompt")
    if (
        not isinstance(candidate, str)
        or not candidate.strip()
        or len(candidate) > _MAX_PROMPT_CHARACTERS
    ):
        raise ConfigError("prompt must be non-empty text up to 1024 characters")
    if any(not character.isprintable() for character in candidate):
        raise ConfigError("prompt must not contain control characters")
    return candidate


def _optional_pulse_name(value: dict[str, Any], key: str) -> str | None:
    if key not in value:
        return None
    candidate = value.get(key)
    if not isinstance(candidate, str) or not _PULSE_OBJECT_NAME.fullmatch(candidate):
        raise ConfigError(f"{key} must be a safe PulseAudio object name")
    return candidate


def _optional_pulse_aec_method(value: dict[str, Any]) -> str | None:
    if "pulse_aec_method" not in value:
        return None
    candidate = value.get("pulse_aec_method")
    if not isinstance(candidate, str) or candidate not in SUPPORTED_PULSE_AEC_METHODS:
        raise ConfigError(_PULSE_AEC_METHOD_ERROR)
    return candidate


def _validate_start_message_size(config: RealtimeConfig) -> None:
    # Keep this encoding identical to WebSocketConnection.send_json. The prompt
    # character bound also keeps the worst-case ensure_ascii expansion below
    # that transport's fixed 16 KiB text ceiling.
    encoded = json.dumps(
        realtime_start_message(config), separators=(",", ":"), ensure_ascii=True
    ).encode()
    if len(encoded) > config.max_message_bytes:
        raise ConfigError("voice and prompt do not fit within max_message_bytes")


def _bounded_float(
    value: dict[str, Any],
    key: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    candidate = value.get(key, default)
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        raise ConfigError(f"{key} must be a number")
    result = float(candidate)
    if not minimum <= result <= maximum:
        raise ConfigError(f"{key} is outside its supported range")
    return result


def _bounded_int(
    value: dict[str, Any],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    candidate = value.get(key, default)
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        raise ConfigError(f"{key} must be an integer")
    if not minimum <= candidate <= maximum:
        raise ConfigError(f"{key} is outside its supported range")
    return candidate
