"""The pause switch - one click stops the schedule, as in the tray menu."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity

from .const import CONF_PAUSED
from .entity import SpeedsterEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import SpeedsterConfigEntry, SpeedsterCoordinator

PARALLEL_UPDATES = 0


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: SpeedsterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the pause switch."""
    async_add_entities([SpeedsterPause(entry.runtime_data)])


class SpeedsterPause(SpeedsterEntity, SwitchEntity):
    """On means the schedule is suspended."""

    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator: SpeedsterCoordinator) -> None:
        """Name the entity."""
        super().__init__(coordinator, "pause")

    @property
    def is_on(self) -> bool:
        """Report whether the schedule is paused."""
        return self.coordinator.paused

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Suspend the schedule."""
        await self.coordinator.async_set_option(CONF_PAUSED, value=True)

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Resume the schedule. The next tick fires anything already due."""
        await self.coordinator.async_set_option(CONF_PAUSED, value=False)
