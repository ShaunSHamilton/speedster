"""Diagnostics: the options, the schedule, and the last result."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import SpeedsterConfigEntry


async def async_get_config_entry_diagnostics(
    _hass: HomeAssistant, entry: SpeedsterConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry. Nothing here is personal."""
    coordinator = entry.runtime_data
    result = coordinator.data
    return {
        "options": coordinator.options,
        "schedule": {
            "paused": coordinator.paused,
            "last_run": coordinator.last_run.isoformat() if coordinator.last_run else None,
            "next_run": coordinator.next_run.isoformat() if coordinator.next_run else None,
            "testing": coordinator.testing,
            "last_error": coordinator.last_error,
        },
        "log": {
            "path": str(coordinator.csv_path),
            "tests_logged": coordinator.test_count,
            "total_bytes": coordinator.total_bytes,
        },
        "last_result": (
            {**asdict(result), "timestamp_utc": result.timestamp_utc.isoformat()}
            if result
            else None
        ),
    }
