"""Trusted local date, time, timezone, and location for realtime voice."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .errors import ProtocolError

GET_CURRENT_TIME_TOOL_NAME = "get_current_time"
GET_CURRENT_TIME_TOOL: dict[str, Any] = {
    "type": "function",
    "name": GET_CURRENT_TIME_TOOL_NAME,
    "description": (
        "Return the exact current local date and time, configured IANA timezone, "
        "UTC offset, and the assistant's configured physical location. Use this "
        "instead of web search whenever the user asks for the current date or time."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}


class AssistantContext:
    """Provide a fresh local clock and deployment location to native sessions."""

    def __init__(self, timezone_name: str, location: str | None) -> None:
        self._timezone_name = timezone_name
        self._timezone = ZoneInfo(timezone_name)
        self._location = location

    @property
    def enabled(self) -> bool:
        return self._location is not None

    @property
    def tools(self) -> tuple[dict[str, Any], ...]:
        return (GET_CURRENT_TIME_TOOL,) if self.enabled else ()

    def owns(self, name: object) -> bool:
        return self.enabled and name == GET_CURRENT_TIME_TOOL_NAME

    def current(self, *, now: datetime | None = None) -> dict[str, str]:
        """Return a compact, exact snapshot in the configured local timezone."""
        instant = (
            datetime.now(self._timezone)
            if now is None
            else now.astimezone(self._timezone)
        )
        offset = instant.strftime("%z")
        normalized_offset = f"{offset[:3]}:{offset[3:]}" if offset else "+00:00"
        result = {
            "local_datetime": instant.isoformat(timespec="seconds"),
            "local_date": instant.date().isoformat(),
            "local_time": instant.strftime("%H:%M:%S"),
            "timezone": self._timezone_name,
            "utc_offset": normalized_offset,
        }
        if self._location is not None:
            result["location"] = self._location
        return result

    def instructions(self) -> str:
        """Build trusted provider-start context with a fresh timestamp."""
        if not self.enabled:
            return ""
        snapshot = self.current()
        return (
            "\n\nTrusted local context (configured by the device owner, not the "
            "user):\n"
            f"Location: {snapshot['location']}\n"
            f"Time zone: {snapshot['timezone']} (UTC{snapshot['utc_offset']})\n"
            f"Local date and time at provider start: {snapshot['local_datetime']}\n"
            "Use this location as the default for local questions. For an exact "
            "current date or time, call get_current_time with {}; do not use web "
            "search for the local clock."
        )

    def call(self, *, name: str, arguments: object) -> dict[str, str]:
        """Execute the sole context tool with strict empty arguments."""
        if not self.owns(name):
            raise ProtocolError("assistant context does not own this tool")
        if not isinstance(arguments, Mapping) or arguments:
            raise ProtocolError("get_current_time requires empty arguments")
        return self.current()
