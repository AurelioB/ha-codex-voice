"""Tests for Codex Voice config-entry lifecycle."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.codex_voice import (
    _async_start_realtime_tool_broker_at_started,
    async_migrate_entry,
    async_setup_entry,
)
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
    startup_unsubscribe = Mock()
    startup_callbacks: list[Any] = []
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
        patch(
            "custom_components.codex_voice.async_at_started",
            side_effect=lambda _hass, callback: (
                startup_callbacks.append(callback) or startup_unsubscribe
            ),
        ),
        patch(
            "custom_components.codex_voice.async_start_realtime_tool_broker"
        ) as start_broker,
    ):
        assert await async_setup_entry(cast("Any", hass), cast("Any", entry))
        start_broker.assert_not_called()
        assert len(startup_callbacks) == 1
        startup_callbacks[0](hass)
        start_broker.assert_called_once()

    entry.async_on_unload.assert_any_call(client.cancel_handoff_release_tasks)
    entry.async_on_unload.assert_any_call(startup_unsubscribe)
    entry.async_on_unload.assert_any_call(update_unsubscribe)
    assert entry.runtime_data is client
    forward_setups.assert_awaited_once()


async def test_realtime_tool_snapshot_waits_for_home_assistant_started(
    hass: HomeAssistant,
) -> None:
    """Full boot cannot capture tools before all integrations finish setup."""
    entry = SimpleNamespace(entry_id="entry-1")
    session = Mock()
    hass.set_state(CoreState.starting)

    with patch(
        "custom_components.codex_voice.async_start_realtime_tool_broker"
    ) as start_broker:
        cancel = _async_start_realtime_tool_broker_at_started(
            hass,
            cast("Any", entry),
            cast("Any", session),
        )
        await hass.async_block_till_done()
        start_broker.assert_not_called()

        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()

        start_broker.assert_called_once_with(hass, entry, session)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        start_broker.assert_called_once()

    cancel()


async def test_unload_cancels_deferred_realtime_tool_snapshot(
    hass: HomeAssistant,
) -> None:
    """Unloading during startup cannot create a broker after the entry is gone."""
    entry = SimpleNamespace(entry_id="entry-1")
    session = Mock()
    hass.set_state(CoreState.starting)

    with patch(
        "custom_components.codex_voice.async_start_realtime_tool_broker"
    ) as start_broker:
        cancel = _async_start_realtime_tool_broker_at_started(
            hass,
            cast("Any", entry),
            cast("Any", session),
        )
        cancel()
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()

    start_broker.assert_not_called()


async def test_realtime_tool_snapshot_starts_immediately_after_boot(
    hass: HomeAssistant,
) -> None:
    """Runtime setup and reload do not wait for another startup event."""
    entry = SimpleNamespace(entry_id="entry-1")
    session = Mock()
    hass.set_state(CoreState.running)

    with patch(
        "custom_components.codex_voice.async_start_realtime_tool_broker"
    ) as start_broker:
        cancel = _async_start_realtime_tool_broker_at_started(
            hass,
            cast("Any", entry),
            cast("Any", session),
        )
        await hass.async_block_till_done()

    start_broker.assert_called_once_with(hass, entry, session)
    cancel()


async def test_migrate_selects_first_authority_and_adds_language(
    hass: HomeAssistant,
) -> None:
    """Legacy profiles gain one deterministic Home Assistant authority."""
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
        True,
        False,
    ]
    assert all(item.data[CONF_REALTIME_LANGUAGE] == "es-MX" for item in conversations)
    assert entry.minor_version == 3
