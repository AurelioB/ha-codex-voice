"""Tests for the Codex Voice config flow."""

from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant import config_entries
from homeassistant.components.homeassistant.const import DATA_EXPOSED_ENTITIES
from homeassistant.components.homeassistant.exposed_entities import ExposedEntities
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.codex_voice.api import BridgeAuthenticationError
from custom_components.codex_voice.const import (
    CONF_ACCESS_TOKEN,
    CONF_BRIDGE_URL,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def mock_exposed_entities(hass: HomeAssistant) -> None:
    """Initialize the core exposed-entities interface used by conversation."""
    hass.data[DATA_EXPOSED_ENTITIES] = cast(
        "ExposedEntities",
        Mock(async_should_expose=Mock(return_value=False)),
    )


async def test_user_flow_creates_three_provider_subentries(
    hass: HomeAssistant,
) -> None:
    """A validated bridge creates Conversation, STT, and TTS providers."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM

    with (
        patch(
            "custom_components.codex_voice.config_flow.BridgeClient.async_health",
            new_callable=AsyncMock,
            return_value={"status": "ok"},
        ),
        patch(
            "custom_components.codex_voice.async_setup_entry",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_BRIDGE_URL: "http://bridge.local:8099/",
                CONF_ACCESS_TOKEN: "bridge-token",
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_BRIDGE_URL: "http://bridge.local:8099",
        CONF_ACCESS_TOKEN: "bridge-token",
    }
    assert [item["subentry_type"] for item in result["subentries"]] == [
        "conversation",
        "stt",
        "tts",
    ]


async def test_user_flow_reports_invalid_auth(hass: HomeAssistant) -> None:
    """An invalid bridge token remains on the form with an auth error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    with patch(
        "custom_components.codex_voice.config_flow.BridgeClient.async_health",
        new_callable=AsyncMock,
        side_effect=BridgeAuthenticationError,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_BRIDGE_URL: "http://bridge.local:8099",
                CONF_ACCESS_TOKEN: "wrong-token",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_duplicate_bridge_is_rejected(hass: HomeAssistant) -> None:
    """The normalized bridge URL is a unique config-entry id."""
    first = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    with (
        patch(
            "custom_components.codex_voice.config_flow.BridgeClient.async_health",
            new_callable=AsyncMock,
            return_value={"status": "ok"},
        ),
        patch(
            "custom_components.codex_voice.async_setup_entry",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await hass.config_entries.flow.async_configure(
            first["flow_id"],
            {
                CONF_BRIDGE_URL: "http://bridge.local:8099",
                CONF_ACCESS_TOKEN: "bridge-token",
            },
        )
        second = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
        )
        second = await hass.config_entries.flow.async_configure(
            second["flow_id"],
            {
                CONF_BRIDGE_URL: "http://bridge.local:8099/",
                CONF_ACCESS_TOKEN: "another-token",
            },
        )

    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_configured"


async def test_reconfigure_updates_bridge_unique_id(hass: HomeAssistant) -> None:
    """Changing bridge URL releases the old unique ID and claims the new one."""
    flow = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    with (
        patch(
            "custom_components.codex_voice.config_flow.BridgeClient.async_health",
            new_callable=AsyncMock,
            return_value={"status": "ok"},
        ),
        patch(
            "custom_components.codex_voice.async_setup_entry",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await hass.config_entries.flow.async_configure(
            flow["flow_id"],
            {
                CONF_BRIDGE_URL: "http://old-bridge.local:8099",
                CONF_ACCESS_TOKEN: "old-token",
            },
        )
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        reconfigure = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            reconfigure["flow_id"],
            {
                CONF_BRIDGE_URL: "http://new-bridge.local:8787/",
                CONF_ACCESS_TOKEN: "new-token",
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == "http://new-bridge.local:8787"
    assert entry.data == {
        CONF_BRIDGE_URL: "http://new-bridge.local:8787",
        CONF_ACCESS_TOKEN: "new-token",
    }


async def test_reconfigure_rejects_another_bridge_unique_id(
    hass: HomeAssistant,
) -> None:
    """Reconfiguration cannot claim another config entry's bridge URL."""
    first = MockConfigEntry(
        domain=DOMAIN,
        unique_id="http://first-bridge.local:8787",
        data={
            CONF_BRIDGE_URL: "http://first-bridge.local:8787",
            CONF_ACCESS_TOKEN: "first-token",
        },
    )
    first.add_to_hass(hass)
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="http://second-bridge.local:8787",
        data={
            CONF_BRIDGE_URL: "http://second-bridge.local:8787",
            CONF_ACCESS_TOKEN: "second-token",
        },
    ).add_to_hass(hass)

    flow = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": first.entry_id,
        },
    )
    with patch(
        "custom_components.codex_voice.config_flow.BridgeClient.async_health",
        new_callable=AsyncMock,
        return_value={"status": "ok"},
    ):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"],
            {
                CONF_BRIDGE_URL: "http://second-bridge.local:8787/",
                CONF_ACCESS_TOKEN: "replacement-token",
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert first.unique_id == "http://first-bridge.local:8787"
    assert first.data[CONF_ACCESS_TOKEN] == "first-token"
