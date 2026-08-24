"""Run test now, and regenerate the report."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    """Set up the buttons."""
    coordinator = entry.runtime_data
    async_add_entities([SpeedsterRunTest(coordinator), SpeedsterBuildReport(coordinator)])


class SpeedsterRunTest(SpeedsterEntity, ButtonEntity):
    """Test immediately, off-schedule, ignoring the gate."""

    def __init__(self, coordinator: SpeedsterCoordinator) -> None:
        """Name the entity."""
        super().__init__(coordinator, "run_test")

    async def async_press(self) -> None:
        """Run one test now."""
        await self.coordinator.async_run_test(scheduled=False)


class SpeedsterBuildReport(SpeedsterEntity, ButtonEntity):
    """Regenerate report.html under <config>/www/speedster/."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SpeedsterCoordinator) -> None:
        """Name the entity."""
        super().__init__(coordinator, "build_report")

    async def async_press(self) -> None:
        """Write the report."""
        await self.coordinator.async_build_report()
