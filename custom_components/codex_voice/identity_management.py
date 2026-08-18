"""Admin-only Home Assistant API and panel for local speaker identity."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Final

import voluptuous as vol
from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant

from .api import BridgeClient, BridgeError
from .const import (
    CONF_REALTIME_AUTHORITY,
    CONF_REALTIME_LANGUAGE,
    CONF_REALTIME_VOICE,
    DEFAULT_REALTIME_LANGUAGE,
    DEFAULT_REALTIME_VOICE,
    DOMAIN,
    SUBENTRY_TYPE_CONVERSATION,
    SUPPORTED_LANGUAGES,
    SUPPORTED_VOICES,
)

_DATA_SETUP: Final = f"{DOMAIN}_identity_management_setup"
_PANEL_URL: Final = "codex-voice"
_STATIC_URL: Final = "/codex_voice_static"
_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_MAX_DISPLAY_NAME = 128

type _Handler = Callable[
    [HomeAssistant, websocket_api.ActiveConnection, dict[str, Any]], Awaitable[None]
]


def _entry(hass: HomeAssistant, entry_id: str) -> ConfigEntry[BridgeClient]:
    entry = hass.config_entries.async_get_entry(entry_id)
    if (
        entry is None
        or entry.domain != DOMAIN
        or not isinstance(entry.runtime_data, BridgeClient)
    ):
        raise ValueError("Codex Voice config entry is not loaded")
    return entry


def _profile_id(value: object) -> str:
    if not isinstance(value, str) or _PROFILE_ID.fullmatch(value) is None:
        raise ValueError("Profile ID may contain letters, digits, _, -, and .")
    return value


def _realtime_authority(entry: ConfigEntry[BridgeClient]) -> ConfigSubentry:
    """Return the sole Conversation subentry that owns realtime preferences."""
    authorities = [
        subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_CONVERSATION
        and subentry.data.get(CONF_REALTIME_AUTHORITY) is True
    ]
    if len(authorities) != 1:
        raise ValueError("Configure exactly one realtime Conversation authority")
    return authorities[0]


def _display_name(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("Display name must be text")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > _MAX_DISPLAY_NAME:
        raise ValueError("Display name must be non-empty and bounded")
    return normalized


def _validate_links(
    payload: Mapping[str, object],
    people: list[dict[str, Any]],
    users: list[dict[str, Any]],
) -> None:
    person_ids = {person["entity_id"] for person in people}
    user_ids = {user["id"] for user in users}
    if (
        payload["ha_person_id"] is not None
        and payload["ha_person_id"] not in person_ids
    ):
        raise ValueError("Selected Home Assistant person no longer exists")
    if payload["ha_user_id"] is not None and payload["ha_user_id"] not in user_ids:
        raise ValueError("Selected Home Assistant user no longer exists")


async def _people_and_users(
    hass: HomeAssistant,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    users = [
        {
            "id": user.id,
            "name": user.name,
            "is_active": user.is_active,
            "is_owner": user.is_owner,
            "system_generated": user.system_generated,
        }
        for user in await hass.auth.async_get_users()
        if not user.system_generated
    ]
    people = [
        {
            "entity_id": state.entity_id,
            "name": state.name,
            "user_id": state.attributes.get("user_id"),
        }
        for state in hass.states.async_all("person")
    ]
    return people, users


async def _send_bridge_result(
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    operation: Awaitable[Mapping[str, Any]],
) -> None:
    try:
        result = await operation
    except (BridgeError, ValueError) as err:
        connection.send_error(msg["id"], "speaker_identity_error", str(err))
    else:
        connection.send_result(msg["id"], dict(result))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/identity/entries"})
@websocket_api.async_response
async def websocket_entries(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """List loaded integration entries for the management panel."""
    result = [
        {"entry_id": entry.entry_id, "title": entry.title}
        for entry in hass.config_entries.async_entries(DOMAIN)
        if isinstance(entry.runtime_data, BridgeClient)
    ]
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/identity/status",
        vol.Required("entry_id"): str,
    }
)
@websocket_api.async_response
async def websocket_status(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return identity state and Home Assistant link candidates."""
    try:
        entry = _entry(hass, msg["entry_id"])
        authority = _realtime_authority(entry)
        status = await entry.runtime_data.async_speaker_identity_status()
        people, users = await _people_and_users(hass)
    except (BridgeError, ValueError) as err:
        connection.send_error(msg["id"], "speaker_identity_error", str(err))
        return
    connection.send_result(
        msg["id"],
        {
            **status,
            "people": people,
            "users": users,
            "assistant_settings": {
                "language": authority.data.get(
                    CONF_REALTIME_LANGUAGE, DEFAULT_REALTIME_LANGUAGE
                ),
                "voice": authority.data.get(
                    CONF_REALTIME_VOICE, DEFAULT_REALTIME_VOICE
                ),
                "languages": list(SUPPORTED_LANGUAGES),
                "voices": list(SUPPORTED_VOICES),
            },
            "integration_url": f"/config/integrations/integration/{DOMAIN}",
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/identity/enrollment/start",
        vol.Required("entry_id"): str,
        vol.Required("speaker_id"): str,
        vol.Required("display_name"): str,
        vol.Optional("ha_person_id", default=None): vol.Any(None, str),
        vol.Optional("ha_user_id", default=None): vol.Any(None, str),
        vol.Required("consent"): bool,
    }
)
@websocket_api.async_response
async def websocket_start_enrollment(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Start collecting one private embedding per subsequent voice session."""
    try:
        entry = _entry(hass, msg["entry_id"])
        payload = {
            "speaker_id": _profile_id(msg["speaker_id"]),
            "display_name": _display_name(msg["display_name"]),
            "ha_person_id": msg["ha_person_id"],
            "ha_user_id": msg["ha_user_id"],
            "consent": msg["consent"],
        }
        people, users = await _people_and_users(hass)
        _validate_links(payload, people, users)
    except (TypeError, ValueError) as err:
        connection.send_error(msg["id"], "invalid_enrollment", str(err))
        return
    await _send_bridge_result(
        connection,
        msg,
        entry.runtime_data.async_start_speaker_enrollment(payload),
    )


_ENTRY_AND_PROFILE_SCHEMA = {
    vol.Required("entry_id"): str,
    vol.Required("speaker_id"): str,
}


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/identity/enrollment/complete",
        **_ENTRY_AND_PROFILE_SCHEMA,
    }
)
@websocket_api.async_response
async def websocket_complete_enrollment(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Build an inactive profile from the completed enrollment."""
    entry = _entry(hass, msg["entry_id"])
    await _send_bridge_result(
        connection,
        msg,
        entry.runtime_data.async_complete_speaker_enrollment(
            _profile_id(msg["speaker_id"])
        ),
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/identity/enrollment/cancel",
        **_ENTRY_AND_PROFILE_SCHEMA,
    }
)
@websocket_api.async_response
async def websocket_cancel_enrollment(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Cancel one pending enrollment."""
    entry = _entry(hass, msg["entry_id"])
    await _send_bridge_result(
        connection,
        msg,
        entry.runtime_data.async_cancel_speaker_enrollment(
            _profile_id(msg["speaker_id"])
        ),
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/identity/profile/update",
        **_ENTRY_AND_PROFILE_SCHEMA,
        vol.Optional("display_name"): str,
        vol.Optional("ha_person_id"): vol.Any(None, str),
        vol.Optional("ha_user_id"): vol.Any(None, str),
        vol.Optional("enabled"): bool,
    }
)
@websocket_api.async_response
async def websocket_update_profile(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Update one profile's links, name, or activation."""
    try:
        entry = _entry(hass, msg["entry_id"])
        payload = {
            key: (_display_name(value) if key == "display_name" else value)
            for key, value in msg.items()
            if key in {"display_name", "ha_person_id", "ha_user_id", "enabled"}
        }
        people, users = await _people_and_users(hass)
        current_links = {
            "ha_person_id": payload.get("ha_person_id"),
            "ha_user_id": payload.get("ha_user_id"),
        }
        _validate_links(current_links, people, users)
    except (TypeError, ValueError) as err:
        connection.send_error(msg["id"], "invalid_profile", str(err))
        return
    if not payload:
        connection.send_error(
            msg["id"], "invalid_profile", "No profile changes were supplied"
        )
        return
    await _send_bridge_result(
        connection,
        msg,
        entry.runtime_data.async_update_speaker_profile(
            _profile_id(msg["speaker_id"]), payload
        ),
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/identity/profile/delete",
        **_ENTRY_AND_PROFILE_SCHEMA,
    }
)
@websocket_api.async_response
async def websocket_delete_profile(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Delete one profile permanently."""
    entry = _entry(hass, msg["entry_id"])
    await _send_bridge_result(
        connection,
        msg,
        entry.runtime_data.async_delete_speaker_profile(_profile_id(msg["speaker_id"])),
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/identity/test/arm",
        vol.Required("entry_id"): str,
        vol.Required("expected_speaker_id"): vol.Any(None, str),
    }
)
@websocket_api.async_response
async def websocket_arm_test(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Reserve the next post-wake sample for held-out validation."""
    entry = _entry(hass, msg["entry_id"])
    expected = msg["expected_speaker_id"]
    if expected is not None:
        expected = _profile_id(expected)
    await _send_bridge_result(
        connection,
        msg,
        entry.runtime_data.async_arm_speaker_identity_test(expected),
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/identity/settings/update",
        vol.Required("entry_id"): str,
        vol.Required("match_threshold"): vol.Coerce(float),
        vol.Required("margin_threshold"): vol.Coerce(float),
    }
)
@websocket_api.async_response
async def websocket_update_settings(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Persist identity score and separation thresholds."""
    entry = _entry(hass, msg["entry_id"])
    await _send_bridge_result(
        connection,
        msg,
        entry.runtime_data.async_update_speaker_identity_settings(
            match_threshold=msg["match_threshold"],
            margin_threshold=msg["margin_threshold"],
        ),
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/assistant/settings/update",
        vol.Required("entry_id"): str,
        vol.Required("language"): vol.In(SUPPORTED_LANGUAGES),
        vol.Required("voice"): vol.In(SUPPORTED_VOICES),
    }
)
@websocket_api.async_response
async def websocket_update_assistant_settings(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Persist default language and voice for subsequent realtime sessions."""
    try:
        entry = _entry(hass, msg["entry_id"])
        authority = _realtime_authority(entry)
    except ValueError as err:
        connection.send_error(msg["id"], "assistant_settings_error", str(err))
        return
    data = dict(authority.data)
    data[CONF_REALTIME_LANGUAGE] = msg["language"]
    data[CONF_REALTIME_VOICE] = msg["voice"]
    hass.config_entries.async_update_subentry(entry, authority, data=data)
    connection.send_result(
        msg["id"],
        {"language": msg["language"], "voice": msg["voice"]},
    )


_COMMANDS: tuple[_Handler, ...] = (
    websocket_entries,
    websocket_status,
    websocket_start_enrollment,
    websocket_complete_enrollment,
    websocket_cancel_enrollment,
    websocket_update_profile,
    websocket_delete_profile,
    websocket_arm_test,
    websocket_update_settings,
    websocket_update_assistant_settings,
)


async def async_setup_identity_management(hass: HomeAssistant) -> None:
    """Register the admin API and panel exactly once per Home Assistant boot."""
    if hass.data.get(_DATA_SETUP):
        return
    for command in _COMMANDS:
        websocket_api.async_register_command(hass, command)

    panel_path = Path(__file__).with_name("frontend") / "codex-voice-panel.js"
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                f"{_STATIC_URL}/codex-voice-panel.js",
                str(panel_path),
                cache_headers=False,
            )
        ]
    )
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=_PANEL_URL,
        webcomponent_name="codex-voice-panel",
        sidebar_title="Codex Voice",
        sidebar_icon="mdi:account-voice",
        module_url=f"{_STATIC_URL}/codex-voice-panel.js?v=2",
        require_admin=True,
        config_panel_domain=DOMAIN,
    )
    hass.data[_DATA_SETUP] = True
