"""ThirdReality in-process Codex realtime client."""

from __future__ import annotations

import importlib
from typing import Any

from .config import (
    DEFAULT_AEC_SINK_VOLUME_CEILING_PERCENT,
    DEFAULT_AEC_TEST_VOLUME_PERCENT,
    DEFAULT_CONFIG_PATH,
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_MAX_SESSION_SECONDS,
    DEFAULT_PLAYBACK_VOLUME_PERCENT,
    DEFAULT_PULSE_AEC_METHOD,
    DEFAULT_PULSE_AEC_SINK,
    DEFAULT_PULSE_AEC_SOURCE,
    DEVICE_WEBRTC_TRANSPORT,
    MAX_REALTIME_VOLUME_PERCENT,
    NATIVE_AEC3_CAPTURE,
    SUPPORTED_PULSE_AEC_METHODS,
    ConfigError,
    RealtimeConfig,
    load_config,
    normalize_wake_phrase,
)

_SESSION_EXPORTS = frozenset(
    {
        "RealtimeSession",
        "SessionState",
        "SubmitResult",
        "prewarm_device_webrtc",
        "shutdown_all_sessions",
    }
)


def __getattr__(name: str) -> Any:
    """Load session support only after early capture selection is complete."""
    if name not in _SESSION_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    session = importlib.import_module(f"{__name__}.session")
    value = getattr(session, name)
    globals()[name] = value
    return value


__all__ = [
    "DEFAULT_AEC_SINK_VOLUME_CEILING_PERCENT",
    "DEFAULT_AEC_TEST_VOLUME_PERCENT",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_IDLE_TIMEOUT_SECONDS",
    "DEFAULT_MAX_SESSION_SECONDS",
    "DEFAULT_PLAYBACK_VOLUME_PERCENT",
    "DEFAULT_PULSE_AEC_METHOD",
    "DEFAULT_PULSE_AEC_SINK",
    "DEFAULT_PULSE_AEC_SOURCE",
    "DEVICE_WEBRTC_TRANSPORT",
    "MAX_REALTIME_VOLUME_PERCENT",
    "NATIVE_AEC3_CAPTURE",
    "SUPPORTED_PULSE_AEC_METHODS",
    "ConfigError",
    "RealtimeConfig",
    "RealtimeSession",
    "SessionState",
    "SubmitResult",
    "load_config",
    "normalize_wake_phrase",
    "prewarm_device_webrtc",
    "shutdown_all_sessions",
]
