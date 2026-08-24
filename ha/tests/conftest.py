"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.speedster.const import DOMAIN, default_options


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: Any,
) -> Generator[None]:
    """Let Home Assistant load custom_components/speedster in every test."""
    yield


@pytest.fixture
def options() -> dict[str, Any]:
    """Defaults, shrunk so tests do not move megabytes."""
    return {
        **default_options(),
        "max_bytes_down": 400000,
        "max_bytes_up": 400000,
        "streams": 1,
        "latency_samples": 2,
        "retry_delay_ms": 0,
        "sample_ms": 10,
        "write_csv": False,
    }


@pytest.fixture
def mock_entry(hass: HomeAssistant, options: dict[str, Any]) -> ConfigEntry:
    """A config entry with the shrunken options."""
    entry = MockConfigEntry(domain=DOMAIN, title="Speedster", data={}, options=options)
    entry.add_to_hass(hass)
    return entry
