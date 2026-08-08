"""Bridge-specific exceptions with safe client-facing messages."""

from __future__ import annotations

from typing import Any


class BridgeError(Exception):
    """Base class for expected bridge failures."""


class BridgeBusyError(BridgeError):
    """Another bridge operation exclusively owns the realtime speech channel."""


class ProtocolError(BridgeError):
    """The app-server or bridge peer violated the expected protocol."""


class RpcError(BridgeError):
    """A JSON-RPC request failed."""

    def __init__(self, method: str, error: Any) -> None:
        self.method = method
        self.error = error
        message = (
            error.get("message", "unknown JSON-RPC error")
            if isinstance(error, dict)
            else str(error)
        )
        super().__init__(f"{method}: {message}")


class AppServerExited(BridgeError):
    """The Codex child process exited or closed its output."""


class WebRtcUnavailable(BridgeError):
    """The optional WebRTC runtime is not installed."""


class AuthenticationRequired(BridgeError):
    """Codex is not signed in with a managed ChatGPT subscription."""
