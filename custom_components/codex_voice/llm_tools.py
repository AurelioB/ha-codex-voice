"""Shared Home Assistant LLM tool schema serialization."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers import llm
from voluptuous_openapi import convert  # type: ignore[import-untyped]


def serialize_llm_tools(
    api_instance: llm.APIInstance | None,
) -> list[dict[str, Any]]:
    """Convert a captured Home Assistant LLM API to bridge-safe schemas."""
    if api_instance is None:
        return []
    return [
        {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": convert(
                tool.parameters,
                custom_serializer=(
                    api_instance.custom_serializer or llm.selector_serializer
                ),
            ),
        }
        for tool in api_instance.tools
    ]
