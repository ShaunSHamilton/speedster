"""Shared entity base."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SpeedsterCoordinator


class SpeedsterEntity(CoordinatorEntity[SpeedsterCoordinator]):
    """Every Speedster entity hangs off one service device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SpeedsterCoordinator, key: str) -> None:
        """Give the entity its unique id and device."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="Speedster",
            model="Cloudflare speed test",
            name="Speedster",
            configuration_url="https://speed.cloudflare.com",
        )
