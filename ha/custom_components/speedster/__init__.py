"""Speedster: scheduled internet speed tests with the data cost written down.

A port of the Windows tray app's measurement engine to Home Assistant. The
engine, its options and its results.csv format are kept identical to the
desktop app so the two produce comparable numbers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import ATTR_IGNORE_GATE, DOMAIN, SERVICE_BUILD_REPORT, SERVICE_RUN_TEST
from .coordinator import SpeedsterConfigEntry, SpeedsterCoordinator

if TYPE_CHECKING:
    from homeassistant.helpers.typing import ConfigType

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

RUN_TEST_SCHEMA = vol.Schema({vol.Optional(ATTR_IGNORE_GATE, default=True): cv.boolean})


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Register the services once, independent of the config entry."""

    def _coordinator() -> SpeedsterCoordinator:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            msg = "Speedster is not set up"
            raise HomeAssistantError(msg)
        return entries[0].runtime_data

    async def _run_test(call: ServiceCall) -> None:
        """Test now, off-schedule."""
        await _coordinator().async_run_test(scheduled=not call.data[ATTR_IGNORE_GATE])

    async def _build_report(_call: ServiceCall) -> ServiceResponse:
        """Regenerate report.html and say where it landed."""
        path = await _coordinator().async_build_report()
        return {"path": str(path), "url": "/local/speedster/report.html"}

    hass.services.async_register(DOMAIN, SERVICE_RUN_TEST, _run_test, schema=RUN_TEST_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_BUILD_REPORT,
        _build_report,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SpeedsterConfigEntry) -> bool:
    """Set up Speedster from a config entry."""
    coordinator = SpeedsterCoordinator(hass, entry)
    await coordinator.async_prepare()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SpeedsterConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def _async_options_updated(_hass: HomeAssistant, entry: SpeedsterConfigEntry) -> None:
    """Pick up new options without reloading - a reload would restart the startup grace."""
    coordinator = entry.runtime_data
    coordinator.async_options_updated()
    coordinator.async_update_listeners()
