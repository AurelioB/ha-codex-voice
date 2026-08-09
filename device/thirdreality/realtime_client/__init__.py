"""ThirdReality in-process Codex realtime client."""

from .config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    RealtimeConfig,
    load_config,
    normalize_wake_phrase,
)
from .session import (
    RealtimeSession,
    SessionState,
    SubmitResult,
    shutdown_all_sessions,
)

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "ConfigError",
    "RealtimeConfig",
    "RealtimeSession",
    "SessionState",
    "SubmitResult",
    "load_config",
    "normalize_wake_phrase",
    "shutdown_all_sessions",
]
