"""Focused tests for trusted realtime location and clock context."""

from datetime import UTC, datetime

import pytest

from bridge.assistant_context import AssistantContext
from bridge.errors import ProtocolError


def test_context_converts_an_instant_to_configured_local_time() -> None:
    context = AssistantContext("America/Mexico_City", "Mexico City, Mexico")

    result = context.current(now=datetime(2026, 8, 13, 18, 34, 56, tzinfo=UTC))

    assert result == {
        "local_datetime": "2026-08-13T12:34:56-06:00",
        "local_date": "2026-08-13",
        "local_time": "12:34:56",
        "timezone": "America/Mexico_City",
        "utc_offset": "-06:00",
        "location": "Mexico City, Mexico",
        "source": "configuration",
    }


def test_home_assistant_context_overrides_configured_fallback() -> None:
    fallback = AssistantContext("UTC", "Configured fallback")

    context = fallback.with_home_assistant(
        timezone_name="America/Cancun",
        location="Casa HA",
        latitude=21.1619,
        longitude=-86.8515,
    )
    result = context.current(now=datetime(2026, 8, 13, 18, 34, 56, tzinfo=UTC))

    assert result["local_datetime"] == "2026-08-13T13:34:56-05:00"
    assert result["timezone"] == "America/Cancun"
    assert result["location"] == "Casa HA"
    assert result["latitude"] == 21.1619
    assert result["longitude"] == -86.8515
    assert result["source"] == "home_assistant"


def test_context_tool_requires_empty_arguments() -> None:
    context = AssistantContext("America/Mexico_City", "Mexico City, Mexico")

    with pytest.raises(ProtocolError, match="requires empty arguments"):
        context.call(name="get_current_time", arguments={"location": "elsewhere"})
