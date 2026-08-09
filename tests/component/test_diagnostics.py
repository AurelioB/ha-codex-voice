"""Tests for Codex Voice diagnostics."""

from unittest.mock import AsyncMock, Mock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.codex_voice.const import (
    CONF_ACCESS_TOKEN,
    CONF_BRIDGE_URL,
    DOMAIN,
)
from custom_components.codex_voice.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_redact_secrets_and_describe_stt_boundary(
    hass: HomeAssistant,
) -> None:
    """Diagnostics describe the recommendation without claiming pipeline state."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BRIDGE_URL: "http://bridge.local:8787",
            CONF_ACCESS_TOKEN: "bridge-secret",
        },
    )
    entry.runtime_data = Mock(
        async_health=AsyncMock(
            return_value={
                "status": "ok",
                "nested": {"refresh_token": "oauth-secret"},
            }
        )
    )
    entry.add_to_hass(hass)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["config_entry"]["data"][CONF_ACCESS_TOKEN] == "**REDACTED**"
    assert result["bridge"]["nested"]["refresh_token"] == "**REDACTED**"
    assert result["recommended_stt_architecture"] == "home-assistant-wyoming-local"
    assert result["codex_stt_adapter"] == ("experimental-conversational-best-effort")
