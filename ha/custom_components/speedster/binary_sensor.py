"""Problem and testing indicators."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SpeedsterConfigEntry, SpeedsterCoordinator
from .entity import SpeedsterEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SpeedsterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities([SpeedsterProblem(coordinator), SpeedsterTesting(coordinator)])


class SpeedsterProblem(SpeedsterEntity, BinarySensorEntity):
    """On when the last run failed. A skip is an explained gap, not a problem."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: SpeedsterCoordinator) -> None:
        """Name the entity."""
        super().__init__(coordinator, "problem")

    @property
    def is_on(self) -> bool:
        """Did the last run fail outright?"""
        error = self.coordinator.last_error
        return bool(error) and not error.startswith("skipped:")

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """The failure text, verbatim."""
        return {"error": self.coordinator.last_error}


class SpeedsterTesting(SpeedsterEntity, BinarySensorEntity):
    """On while a test is in flight - the blue tray icon."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SpeedsterCoordinator) -> None:
        """Name the entity."""
        super().__init__(coordinator, "testing")

    @property
    def is_on(self) -> bool:
        """Is a test running right now?"""
        return self.coordinator.testing
