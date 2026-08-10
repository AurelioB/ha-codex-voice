"""Tests for Codex Voice config-entry lifecycle."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.codex_voice import async_migrate_entry, async_setup_entry
from custom_components.codex_voice.const import (
    CONF_ACCESS_TOKEN,
    CONF_BRIDGE_URL,
    CONF_REALTIME_AUTHORITY,
    CONF_REALTIME_LANGUAGE,
    DOMAIN,
)


async def test_setup_owns_handoff_release_tasks_until_unload() -> None:
    """The config entry cancels its client's private cleanup jobs on unload."""
    client = SimpleNamespace(
        async_health=AsyncMock(return_value={"status": "ok"}),
        cancel_handoff_release_tasks=Mock(),
    )
    forward_setups = AsyncMock()
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_forward_entry_setups=forward_setups)
    )
    update_unsubscribe = Mock()
    entry = SimpleNamespace(
        data={
            CONF_BRIDGE_URL: "http://bridge.local:8787",
            CONF_ACCESS_TOKEN: "bridge-token",
        },
        runtime_data=None,
        entry_id="entry-1",
        subentries={},
        async_on_unload=Mock(),
        add_update_listener=Mock(return_value=update_unsubscribe),
    )

    with (
        patch("custom_components.codex_voice.BridgeClient", return_value=client),
        patch(
            "custom_components.codex_voice.async_get_clientsession",
            return_value=Mock(),
        ),
    ):
        assert await async_setup_entry(cast("Any", hass), cast("Any", entry))

    entry.async_on_unload.assert_any_call(client.cancel_handoff_release_tasks)
    entry.async_on_unload.assert_any_call(update_unsubscribe)
    assert entry.runtime_data is client
    forward_setups.assert_awaited_once()


async def test_migrate_requires_explicit_authority_opt_in_and_adds_language(
    hass: HomeAssistant,
) -> None:
    """Legacy profiles do not silently gain a Home Assistant control surface."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=1,
        data={CONF_BRIDGE_URL: "http://bridge", CONF_ACCESS_TOKEN: "token"},
        subentries_data=[
            {
                "data": {"model": "first"},
                "subentry_type": "conversation",
                "title": "First",
                "unique_id": None,
            },
            {
                "data": {"model": "second"},
                "subentry_type": "conversation",
                "title": "Second",
                "unique_id": None,
            },
            {
                "data": {"voice": "cove"},
                "subentry_type": "tts",
                "title": "TTS",
                "unique_id": None,
            },
        ],
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    conversations = [
        item
        for item in entry.subentries.values()
        if item.subentry_type == "conversation"
    ]
    assert [item.data[CONF_REALTIME_AUTHORITY] for item in conversations] == [
        False,
        False,
    ]
    assert all(item.data[CONF_REALTIME_LANGUAGE] == "es-MX" for item in conversations)
    assert entry.minor_version == 2
