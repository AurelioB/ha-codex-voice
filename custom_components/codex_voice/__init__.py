"""The Codex Voice integration."""

from __future__ import annotations

import logging

from awesomeversion import AwesomeVersion
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    BridgeAuthenticationError,
    BridgeClient,
    BridgeConnectionError,
    BridgeError,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_BRIDGE_URL,
    CONF_REALTIME_AUTHORITY,
    CONF_REALTIME_LANGUAGE,
    DEFAULT_REALTIME_LANGUAGE,
    MIN_HA_VERSION,
    PLATFORMS,
    SUBENTRY_TYPE_CONVERSATION,
)
from .realtime_tools import async_start_realtime_tool_broker

_LOGGER = logging.getLogger(__name__)

type CodexVoiceConfigEntry = ConfigEntry[BridgeClient]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CodexVoiceConfigEntry,
) -> bool:
    """Set up Codex Voice from a config entry."""
    if AwesomeVersion(HA_VERSION) < AwesomeVersion(MIN_HA_VERSION):
        raise ConfigEntryError(
            translation_domain="codex_voice",
            translation_key="unsupported_home_assistant_version",
            translation_placeholders={"minimum_version": MIN_HA_VERSION},
        )

    session = async_get_clientsession(hass)
    client = BridgeClient(
        session,
        entry.data[CONF_BRIDGE_URL],
        entry.data[CONF_ACCESS_TOKEN],
    )
    entry.async_on_unload(client.cancel_handoff_release_tasks)
    try:
        await client.async_health()
    except BridgeAuthenticationError as err:
        raise ConfigEntryAuthFailed from err
    except BridgeConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err
    except BridgeError as err:
        raise ConfigEntryError(str(err)) from err

    entry.runtime_data = client
    async_start_realtime_tool_broker(hass, entry, session)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_migrate_entry(
    hass: HomeAssistant,
    entry: CodexVoiceConfigEntry,
) -> bool:
    """Add opt-in realtime authority fields and Mexican Spanish language."""
    if entry.version != 1:
        return False
    if entry.minor_version >= 2:
        return True

    conversations = [
        subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_CONVERSATION
    ]
    for subentry in conversations:
        data = dict(subentry.data)
        # Existing installations were explicitly chat-only on the device path.
        # Migration must not silently grant a new Home Assistant control surface.
        data.setdefault(CONF_REALTIME_AUTHORITY, False)
        data.setdefault(CONF_REALTIME_LANGUAGE, DEFAULT_REALTIME_LANGUAGE)
        hass.config_entries.async_update_subentry(entry, subentry, data=data)

    hass.config_entries.async_update_entry(entry, minor_version=2)
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: CodexVoiceConfigEntry,
) -> bool:
    """Unload a Codex Voice config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(
    hass: HomeAssistant,
    entry: CodexVoiceConfigEntry,
) -> None:
    """Reload Codex Voice after configuration changes."""
    await hass.config_entries.async_reload(entry.entry_id)
