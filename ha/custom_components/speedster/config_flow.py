"""Config and options flow.

The options form is the settings.ini table, grouped so the everyday knobs are
visible and the measurement internals are collapsed. Bounds come from
``const.OPTIONS``, so the form cannot offer a value the engine would clamp away.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)

from .const import (
    CONF_DISCARD_MS,
    CONF_DISCARD_PERCENT,
    CONF_GATE_ENTITY,
    CONF_GATE_STATE,
    CONF_INTERVAL_MINUTES,
    CONF_LATENCY_SAMPLES,
    CONF_MAX_BYTES_DOWN,
    CONF_MAX_BYTES_UP,
    CONF_MAX_TEST_SECONDS,
    CONF_MIN_WINDOW_MS,
    CONF_READ_BUFFER_BYTES,
    CONF_REQUEST_BYTES_MAX,
    CONF_RETRY_COUNT,
    CONF_RETRY_DELAY_MS,
    CONF_SAMPLE_MS,
    CONF_STARTUP_DELAY_SECONDS,
    CONF_STREAMS,
    CONF_TARGET_SECONDS_DOWN,
    CONF_TARGET_SECONDS_UP,
    CONF_WRITE_CHUNK_BYTES,
    CONF_WRITE_CSV,
    DOMAIN,
    OPTIONS,
    default_options,
)

if TYPE_CHECKING:
    from .coordinator import SpeedsterConfigEntry

#: Which options appear in which collapsible section of the options form.
SECTIONS: dict[str, tuple[str, ...]] = {
    "schedule": (CONF_INTERVAL_MINUTES, CONF_STARTUP_DELAY_SECONDS, CONF_WRITE_CSV),
    "gate": (CONF_GATE_ENTITY, CONF_GATE_STATE),
    "measurement": (
        CONF_TARGET_SECONDS_DOWN,
        CONF_TARGET_SECONDS_UP,
        CONF_MAX_BYTES_DOWN,
        CONF_MAX_BYTES_UP,
        CONF_STREAMS,
        CONF_MAX_TEST_SECONDS,
        CONF_LATENCY_SAMPLES,
    ),
    "tuning": (
        CONF_DISCARD_MS,
        CONF_DISCARD_PERCENT,
        CONF_MIN_WINDOW_MS,
        CONF_SAMPLE_MS,
        CONF_READ_BUFFER_BYTES,
        CONF_WRITE_CHUNK_BYTES,
        CONF_REQUEST_BYTES_MAX,
        CONF_RETRY_COUNT,
        CONF_RETRY_DELAY_MS,
    ),
}

#: Sections the user has to open to see. Everything here has a working default.
COLLAPSED = ("gate", "measurement", "tuning")


def _selector(key: str) -> Any:
    """Build a selector matching the option's type and clamp bounds."""
    default, low, high = OPTIONS[key]
    if key == CONF_GATE_ENTITY:
        return EntitySelector()
    if key == CONF_GATE_STATE:
        return TextSelector()
    if isinstance(default, bool):
        return BooleanSelector()
    step = 0.1 if isinstance(default, float) else 1
    return NumberSelector(
        NumberSelectorConfig(min=low, max=high, step=step, mode=NumberSelectorMode.BOX)
    )


def _options_schema(current: dict[str, Any]) -> vol.Schema:
    """Build the sectioned options form, prefilled with the current values."""
    schema: dict[Any, Any] = {}
    for name, keys in SECTIONS.items():
        fields = {}
        for key in keys:
            suggested = current.get(key)
            if key == CONF_GATE_ENTITY:
                # An empty gate means "always test", and an entity selector rejects "",
                # so the field has to be omittable rather than required-and-blank.
                marker: Any = vol.Optional(key, description={"suggested_value": suggested or None})
            else:
                marker = vol.Required(key, description={"suggested_value": suggested})
            fields[marker] = _selector(key)
        schema[vol.Required(name)] = section(vol.Schema(fields), {"collapsed": name in COLLAPSED})
    return vol.Schema(schema)


def _flatten(user_input: dict[str, Any]) -> dict[str, Any]:
    """Turn the sectioned form back into flat options, coercing ints back to int."""
    flat: dict[str, Any] = {}
    for name, keys in SECTIONS.items():
        submitted = user_input.get(name, {})
        for key, value in submitted.items():
            default = OPTIONS[key][0]
            if isinstance(default, bool) or not isinstance(default, int):
                flat[key] = value
            else:
                flat[key] = int(value)
        # An omitted optional field is a cleared field, not an unchanged one -
        # otherwise the gate could never be removed once set.
        for key in keys:
            if key not in submitted and isinstance(OPTIONS[key][0], str):
                flat[key] = ""
    return flat


class SpeedsterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Nothing to discover and nothing to authenticate - just confirm and go."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Create the single entry."""
        if user_input is not None:
            options = default_options()
            options[CONF_INTERVAL_MINUTES] = int(user_input[CONF_INTERVAL_MINUTES])
            return self.async_create_entry(title="Speedster", data={}, options=options)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_INTERVAL_MINUTES, default=OPTIONS[CONF_INTERVAL_MINUTES][0]
                    ): _selector(CONF_INTERVAL_MINUTES)
                }
            ),
            description_placeholders={"host": "speed.cloudflare.com"},
        )

    @staticmethod
    def async_get_options_flow(_entry: SpeedsterConfigEntry) -> SpeedsterOptionsFlow:
        """Return the options flow."""
        return SpeedsterOptionsFlow()


class SpeedsterOptionsFlow(OptionsFlow):
    """Every settings.ini knob, in one sectioned form."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show and save the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={**self.config_entry.options, **_flatten(user_input)}
            )

        current = {**default_options(), **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_options_schema(current))
