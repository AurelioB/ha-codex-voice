"""Base entities for Codex Voice."""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import Entity

from . import CodexVoiceConfigEntry
from .const import DOMAIN, MANUFACTURER


class CodexVoiceEntity(Entity):
    """Base entity attached to a Codex Voice config subentry."""

    _attr_has_entity_name = True
    _attr_name: str | None = None

    def __init__(
        self,
        entry: CodexVoiceConfigEntry,
        subentry: ConfigSubentry,
    ) -> None:
        """Initialize the entity."""
        self.entry = entry
        self.subentry = subentry
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            manufacturer=MANUFACTURER,
            model="Codex subscription bridge",
            name=subentry.title,
            entry_type=dr.DeviceEntryType.SERVICE,
        )
