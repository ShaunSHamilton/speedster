"""Sensors for the last test and the running cost of monitoring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfDataRate, UnitOfInformation, UnitOfTime

from .entity import SpeedsterEntity

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import SpeedsterConfigEntry, SpeedsterCoordinator

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SpeedsterSensorDescription(SensorEntityDescription):
    """A sensor and how to read its value off the coordinator."""

    value_fn: Callable[[SpeedsterCoordinator], Any]
    attrs_fn: Callable[[SpeedsterCoordinator], dict[str, Any]] | None = None
    #: True when the value comes from the last test rather than from the schedule.
    needs_result: bool = True


def _seconds_attrs(key: str) -> Callable[[SpeedsterCoordinator], dict[str, Any]]:
    """Expose the window a rate was measured over - a short sample is worth seeing."""

    def _attrs(coordinator: SpeedsterCoordinator) -> dict[str, Any]:
        result = coordinator.data
        if result is None:
            return {}
        return {"measured_seconds": round(getattr(result, key), 3)}

    return _attrs


def _status(coordinator: SpeedsterCoordinator) -> str:
    """Return one word for what the last run did."""
    if coordinator.testing:
        return "testing"
    if coordinator.last_error:
        return "skipped" if coordinator.last_error.startswith("skipped:") else "failed"
    if coordinator.data is None:
        return "unknown"
    return "ok"


SENSORS: tuple[SpeedsterSensorDescription, ...] = (
    SpeedsterSensorDescription(
        key="download",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda c: c.data.down_mbps if c.data else None,
        attrs_fn=_seconds_attrs("down_seconds"),
    ),
    SpeedsterSensorDescription(
        key="upload",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda c: c.data.up_mbps if c.data else None,
        attrs_fn=_seconds_attrs("up_seconds"),
    ),
    SpeedsterSensorDescription(
        key="latency",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda c: c.data.latency_ms if c.data else None,
    ),
    SpeedsterSensorDescription(
        key="jitter",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda c: c.data.jitter_ms if c.data else None,
    ),
    SpeedsterSensorDescription(
        key="test_data",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.MEGABYTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda c: c.data.total_bytes if c.data else None,
        attrs_fn=lambda c: (
            {"down_bytes": c.data.down_bytes, "up_bytes": c.data.up_bytes} if c.data else {}
        ),
    ),
    SpeedsterSensorDescription(
        key="total_data",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        needs_result=False,
        value_fn=lambda c: c.total_bytes,
        attrs_fn=lambda c: {"tests_logged": c.test_count},
    ),
    SpeedsterSensorDescription(
        key="projected_monthly_data",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=2,
        value_fn=lambda c: c.projected_monthly_bytes,
    ),
    SpeedsterSensorDescription(
        key="last_test",
        device_class=SensorDeviceClass.TIMESTAMP,
        needs_result=False,
        value_fn=lambda c: c.last_run,
    ),
    SpeedsterSensorDescription(
        key="next_test",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        needs_result=False,
        value_fn=lambda c: c.next_run,
    ),
    SpeedsterSensorDescription(
        key="status",
        device_class=SensorDeviceClass.ENUM,
        options=["ok", "testing", "skipped", "failed", "unknown"],
        needs_result=False,
        value_fn=_status,
        attrs_fn=lambda c: {
            "error": c.last_error,
            "server": c.data.server if c.data else "",
            "engine": "cloudflare",
        },
    ),
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: SpeedsterConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    coordinator = entry.runtime_data
    async_add_entities(SpeedsterSensor(coordinator, description) for description in SENSORS)


class SpeedsterSensor(SpeedsterEntity, SensorEntity):
    """One reading from the last test, or one figure derived from the log."""

    entity_description: SpeedsterSensorDescription

    def __init__(
        self, coordinator: SpeedsterCoordinator, description: SpeedsterSensorDescription
    ) -> None:
        """Attach the description."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | str | datetime | None:
        """Current value, or None until there is a result to report."""
        if self.entity_description.needs_result and self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Context that does not deserve an entity of its own."""
        if (attrs_fn := self.entity_description.attrs_fn) is None:
            return None
        return attrs_fn(self.coordinator)
