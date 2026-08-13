"""Trusted local date, time, timezone, and location for realtime voice."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from math import isfinite
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

    def __init__(
        self,
        timezone_name: str,
        location: str | None,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        source: str = "configuration",
    ) -> None:
        if (latitude is None) != (longitude is None):
            raise ValueError("assistant location coordinates must be paired")
        if (
            latitude is not None
            and longitude is not None
            and (
                not isfinite(latitude)
                or not isfinite(longitude)
                or not -90 <= latitude <= 90
                or not -180 <= longitude <= 180
            )
        ):
            raise ValueError("assistant location coordinates are invalid")
        self._timezone_name = timezone_name
        self._timezone = ZoneInfo(timezone_name)
        self._location = location
        self._latitude = latitude
        self._longitude = longitude
        self._source = source

    @property
    def enabled(self) -> bool:
        return self._location is not None

    @property
    def tools(self) -> tuple[dict[str, Any], ...]:
        return (GET_CURRENT_TIME_TOOL,) if self.enabled else ()

    def owns(self, name: object) -> bool:
        return self.enabled and name == GET_CURRENT_TIME_TOOL_NAME

    def with_home_assistant(
        self,
        *,
        timezone_name: str | None,
        location: str | None,
        latitude: float | None,
        longitude: float | None,
    ) -> AssistantContext:
        """Return one immutable session view, preferring available HA values."""
        uses_home_assistant = any(
            value is not None
            for value in (timezone_name, location, latitude, longitude)
        )
        return AssistantContext(
            timezone_name or self._timezone_name,
            location or self._location,
            latitude=latitude,
            longitude=longitude,
            source="home_assistant" if uses_home_assistant else self._source,
        )

    def current(self, *, now: datetime | None = None) -> dict[str, str | float]:
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
            "source": self._source,
        }
        if self._location is not None:
            result["location"] = self._location
        if self._latitude is not None and self._longitude is not None:
            result["latitude"] = self._latitude
            result["longitude"] = self._longitude
        return result

    def instructions(self) -> str:
        """Build trusted provider-start context with a fresh timestamp."""
        if not self.enabled:
            return ""
        snapshot = self.current()
        coordinate_context = (
            f"\nCoordinates: {snapshot['latitude']}, {snapshot['longitude']}"
            if "latitude" in snapshot
            else ""
        )
        return (
            "\n\nTrusted local context (configured by the device owner, not the "
            "user):\n"
            f"Location: {snapshot['location']}"
            f"{coordinate_context}\n"
            f"Time zone: {snapshot['timezone']} (UTC{snapshot['utc_offset']})\n"
            f"Context source: {snapshot['source']}\n"
            f"Local date and time at provider start: {snapshot['local_datetime']}\n"
            "Use this location as the default for local questions. For an exact "
            "current date or time, call get_current_time with {}; do not use web "
            "search for the local clock."
        )

    def call(self, *, name: str, arguments: object) -> dict[str, str | float]:
        """Execute the sole context tool with strict empty arguments."""
        if not self.owns(name):
            raise ProtocolError("assistant context does not own this tool")
        if not isinstance(arguments, Mapping) or arguments:
            raise ProtocolError("get_current_time requires empty arguments")
        return self.current()
