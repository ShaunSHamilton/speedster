"""Setup, scheduling and service tests."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.speedster.const import DOMAIN, SERVICE_RUN_TEST
from custom_components.speedster.engine import SpeedResult

RESULT = SpeedResult(down_mbps=9.1, up_mbps=2.2, latency_ms=21.0, down_bytes=4_000_000)


async def _setup(hass: HomeAssistant, entry: ConfigEntry) -> AsyncMock:
    """Set the entry up with the engine stubbed out."""
    run = AsyncMock(return_value=RESULT)
    with patch("custom_components.speedster.engine.SpeedsterEngine.run", run):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return run


async def test_entities_created(hass: HomeAssistant, mock_entry: ConfigEntry) -> None:
    """A fresh entry produces the full entity set."""
    await _setup(hass, mock_entry)
    assert mock_entry.state is ConfigEntryState.LOADED
    for entity_id in (
        "sensor.speedster_download",
        "sensor.speedster_upload",
        "sensor.speedster_latency",
        "sensor.speedster_jitter",
        "sensor.speedster_total_data_used",
        "sensor.speedster_status",
        "binary_sensor.speedster_problem",
        "switch.speedster_pause",
        "button.speedster_run_test_now",
    ):
        assert hass.states.get(entity_id) is not None, entity_id


async def test_startup_delay_then_first_test(
    hass: HomeAssistant, mock_entry: ConfigEntry
) -> None:
    """Nothing runs during the grace period; the first tick past it fires a test."""
    run = await _setup(hass, mock_entry)
    coordinator = mock_entry.runtime_data

    with patch("custom_components.speedster.engine.SpeedsterEngine.run", run):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
        await hass.async_block_till_done()
        assert run.call_count == 0  # startup_delay_seconds defaults to 60

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=91))
        await hass.async_block_till_done()
        assert run.call_count == 1

    assert coordinator.last_run is not None
    assert hass.states.get("sensor.speedster_download").state == "9.1"


async def test_interval_is_not_double_fired(
    hass: HomeAssistant, mock_entry: ConfigEntry
) -> None:
    """last_run plus the interval is the gate, so ticks in between do nothing."""
    run = await _setup(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    coordinator.last_run = dt_util.utcnow()
    coordinator._not_before = dt_util.utcnow()  # noqa: SLF001 - skip the startup grace

    with patch("custom_components.speedster.engine.SpeedsterEngine.run", run):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=59))
        await hass.async_block_till_done()
        assert run.call_count == 0


async def test_pause_stops_the_schedule(hass: HomeAssistant, mock_entry: ConfigEntry) -> None:
    """The pause switch suspends the schedule without unloading anything."""
    run = await _setup(hass, mock_entry)
    coordinator = mock_entry.runtime_data
    coordinator._not_before = dt_util.utcnow()  # noqa: SLF001

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.speedster_pause"}, blocking=True
    )
    assert coordinator.paused

    with patch("custom_components.speedster.engine.SpeedsterEngine.run", run):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
        await hass.async_block_till_done()
        assert run.call_count == 0


@pytest.mark.parametrize(
    ("gate_state", "expect_calls"),
    [("on", 1), ("off", 0)],
)
async def test_gate_blocks_scheduled_test(
    hass: HomeAssistant,
    options: dict[str, Any],
    gate_state: str,
    expect_calls: int,
) -> None:
    """A scheduled test only runs while the gate entity is in the required state."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    hass.states.async_set("binary_sensor.wan_ok", gate_state)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={
            **options,
            "gate_entity": "binary_sensor.wan_ok",
            "gate_state": "on",
            "startup_delay_seconds": 0,
        },
    )
    entry.add_to_hass(hass)
    run = await _setup(hass, entry)

    with patch("custom_components.speedster.engine.SpeedsterEngine.run", run):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
        await hass.async_block_till_done()

    assert run.call_count == expect_calls
    # Either way the interval is burned, so a closed gate cannot cause a retry storm.
    assert entry.runtime_data.last_run is not None


async def test_run_test_service_ignores_the_gate(
    hass: HomeAssistant, options: dict[str, Any]
) -> None:
    """A manual run happens whatever the gate says."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    hass.states.async_set("binary_sensor.wan_ok", "off")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={**options, "gate_entity": "binary_sensor.wan_ok", "gate_state": "on"},
    )
    entry.add_to_hass(hass)
    run = await _setup(hass, entry)

    with patch("custom_components.speedster.engine.SpeedsterEngine.run", run):
        await hass.services.async_call(DOMAIN, SERVICE_RUN_TEST, {}, blocking=True)

    assert run.call_count == 1


async def test_unload(hass: HomeAssistant, mock_entry: ConfigEntry) -> None:
    """Unloading closes the measurement session and removes the entities."""
    await _setup(hass, mock_entry)
    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_entry.state is ConfigEntryState.NOT_LOADED
