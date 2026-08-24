"""Config and options flow tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

from custom_components.speedster.const import CONF_INTERVAL_MINUTES, DOMAIN, OPTIONS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def test_user_flow_seeds_every_default(hass: HomeAssistant) -> None:
    """The entry starts with a complete, valid option set."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_INTERVAL_MINUTES: 15}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_INTERVAL_MINUTES] == 15
    assert set(result["options"]) == set(OPTIONS)


async def test_single_instance(hass: HomeAssistant, mock_entry: Any) -> None:
    """Only one Speedster entry makes sense."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_options_flow_flattens_sections(hass: HomeAssistant, mock_entry: Any) -> None:
    """Sectioned input is stored flat, with numbers coerced back to int."""
    from unittest.mock import AsyncMock, patch

    with patch(
        "custom_components.speedster.engine.SpeedsterEngine.run", AsyncMock(return_value=None)
    ):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    current = {**mock_entry.options}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "schedule": {
                CONF_INTERVAL_MINUTES: 30.0,
                "startup_delay_seconds": 0,
                "write_csv": False,
            },
            # gate_entity omitted: an empty gate means always test.
            "gate": {"gate_state": "on"},
            "measurement": {
                "target_seconds_down": 5.0,
                "target_seconds_up": current["target_seconds_up"],
                "max_bytes_down": current["max_bytes_down"],
                "max_bytes_up": current["max_bytes_up"],
                "streams": current["streams"],
                "max_test_seconds": current["max_test_seconds"],
                "latency_samples": current["latency_samples"],
            },
            "tuning": {
                "discard_ms": 500,
                "discard_percent": 25,
                "min_window_ms": 1200,
                "sample_ms": 100,
                "read_buffer_bytes": 65536,
                "write_chunk_bytes": 65536,
                "request_bytes_max": 25000000,
                "retry_count": 2,
                "retry_delay_ms": 0,
            },
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_entry.options[CONF_INTERVAL_MINUTES] == 30
    assert isinstance(mock_entry.options[CONF_INTERVAL_MINUTES], int)
    assert mock_entry.options["target_seconds_down"] == 5.0
    assert mock_entry.options["gate_entity"] == ""


async def test_out_of_range_options_are_clamped(hass: HomeAssistant, mock_entry: Any) -> None:
    """A value outside the documented range is pulled back, not rejected."""
    from unittest.mock import AsyncMock, patch

    hass.config_entries.async_update_entry(
        mock_entry, options={**mock_entry.options, "streams": 999}
    )
    with patch(
        "custom_components.speedster.engine.SpeedsterEngine.run", AsyncMock(return_value=None)
    ):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_entry.runtime_data.options["streams"] == 64
