"""Tests for the admin-only Codex Voice identity management API."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
)

from custom_components.codex_voice.api import BridgeClient
from custom_components.codex_voice.const import (
    CONF_ACCESS_TOKEN,
    CONF_BRIDGE_URL,
    CONF_REALTIME_AUTHORITY,
    CONF_REALTIME_LANGUAGE,
    CONF_REALTIME_VOICE,
    DOMAIN,
)
from custom_components.codex_voice.identity_management import (
    async_setup_identity_management,
)


async def _setup(
    hass: HomeAssistant,
    hass_ws_client: Any,
) -> tuple[Any, BridgeClient, MockConfigEntry]:
    client = BridgeClient(AsyncMock(), "http://bridge.local:8787", "bridge-token")
    client.async_speaker_identity_status = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "status": "ok",
            "profiles": [],
            "enrollments": [],
            "settings": {"match_threshold": 0.55, "margin_threshold": 0.08},
            "required_samples": 5,
            "raw_audio_retained": False,
        }
    )
    client.async_start_speaker_enrollment = AsyncMock(  # type: ignore[method-assign]
        return_value={"speaker_id": "owner", "sample_count": 0}
    )
    client.async_update_speaker_profile = AsyncMock(  # type: ignore[method-assign]
        return_value={"speaker_id": "owner", "enabled": True}
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Codex Voice",
        data={CONF_BRIDGE_URL: client.base_url, CONF_ACCESS_TOKEN: "bridge-token"},
        subentries_data=[
            {
                "data": {
                    CONF_REALTIME_AUTHORITY: True,
                    CONF_REALTIME_LANGUAGE: "es-MX",
                    CONF_REALTIME_VOICE: "cove",
                },
                "subentry_type": "conversation",
                "title": "Conversation",
                "unique_id": None,
            }
        ],
    )
    entry.runtime_data = client
    entry.add_to_hass(hass)
    hass.http = SimpleNamespace(async_register_static_paths=AsyncMock())
    with patch(
        "custom_components.codex_voice.identity_management.panel_custom.async_register_panel",
        new_callable=AsyncMock,
    ):
        await async_setup_identity_management(hass)
    return await hass_ws_client(hass), client, entry


async def test_status_includes_home_assistant_people_and_users(
    hass: HomeAssistant,
    hass_ws_client: Any,
) -> None:
    """The panel receives only live HA Person/user link candidates."""
    websocket, client, entry = await _setup(hass, hass_ws_client)
    owner = await hass.auth.async_create_user("Aurelio")
    hass.states.async_set(
        "person.aurelio",
        "home",
        {"friendly_name": "Aurelio", "user_id": owner.id},
    )

    await websocket.send_json(
        {
            "id": 1,
            "type": "codex_voice/identity/status",
            "entry_id": entry.entry_id,
        }
    )
    response = await websocket.receive_json()

    assert response["success"] is True
    assert response["result"]["raw_audio_retained"] is False
    assert response["result"]["people"] == [
        {
            "entity_id": "person.aurelio",
            "name": "Aurelio",
            "user_id": owner.id,
        }
    ]
    assert response["result"]["assistant_settings"]["language"] == "es-MX"
    assert response["result"]["assistant_settings"]["voice"] == "cove"
    assert any(user["id"] == owner.id for user in response["result"]["users"])
    client.async_speaker_identity_status.assert_awaited_once()  # type: ignore[attr-defined]


async def test_admin_updates_realtime_voice_and_language(
    hass: HomeAssistant,
    hass_ws_client: Any,
) -> None:
    """The panel persists next-session preferences on the authority subentry."""
    websocket, _client, entry = await _setup(hass, hass_ws_client)

    await websocket.send_json(
        {
            "id": 9,
            "type": "codex_voice/assistant/settings/update",
            "entry_id": entry.entry_id,
            "language": "en-US",
            "voice": "ember",
        }
    )
    response = await websocket.receive_json()

    assert response == {
        "id": 9,
        "type": "result",
        "success": True,
        "result": {"language": "en-US", "voice": "ember"},
    }
    authority = next(iter(entry.subentries.values()))
    assert authority.data[CONF_REALTIME_LANGUAGE] == "en-US"
    assert authority.data[CONF_REALTIME_VOICE] == "ember"


async def test_enrollment_requires_admin_selected_live_person_and_consent(
    hass: HomeAssistant,
    hass_ws_client: Any,
) -> None:
    """Enrollment and profile edits reject stale Home Assistant links."""
    websocket, client, entry = await _setup(hass, hass_ws_client)
    owner = await hass.auth.async_create_user("Aurelio")
    hass.states.async_set(
        "person.aurelio",
        "home",
        {"friendly_name": "Aurelio", "user_id": owner.id},
    )

    await websocket.send_json(
        {
            "id": 2,
            "type": "codex_voice/identity/enrollment/start",
            "entry_id": entry.entry_id,
            "speaker_id": "owner",
            "display_name": "Aurelio",
            "ha_person_id": "person.aurelio",
            "ha_user_id": owner.id,
            "consent": True,
        }
    )
    response = await websocket.receive_json()

    assert response["success"] is True
    client.async_start_speaker_enrollment.assert_awaited_once_with(  # type: ignore[attr-defined]
        {
            "speaker_id": "owner",
            "display_name": "Aurelio",
            "ha_person_id": "person.aurelio",
            "ha_user_id": owner.id,
            "consent": True,
        }
    )

    await websocket.send_json(
        {
            "id": 3,
            "type": "codex_voice/identity/enrollment/start",
            "entry_id": entry.entry_id,
            "speaker_id": "visitor",
            "display_name": "Visitor",
            "ha_person_id": "person.missing",
            "ha_user_id": None,
            "consent": True,
        }
    )
    rejected = await websocket.receive_json()
    assert rejected["success"] is False
    assert rejected["error"]["code"] == "invalid_enrollment"

    await websocket.send_json(
        {
            "id": 4,
            "type": "codex_voice/identity/profile/update",
            "entry_id": entry.entry_id,
            "speaker_id": "owner",
            "ha_person_id": "person.missing",
        }
    )
    stale_link = await websocket.receive_json()
    assert stale_link["success"] is False
    assert stale_link["error"]["code"] == "invalid_profile"
    client.async_update_speaker_profile.assert_not_awaited()  # type: ignore[attr-defined]
