"""Tests for Codex Voice config-entry lifecycle."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

from custom_components.codex_voice import async_setup_entry
from custom_components.codex_voice.const import CONF_ACCESS_TOKEN, CONF_BRIDGE_URL


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
