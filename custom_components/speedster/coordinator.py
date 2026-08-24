"""Scheduling and test orchestration.

The schedule is a port of ``TrayContext.Tick`` in ``Speedster.cs`` rather than a
plain ``update_interval``, and for the same reason: the next run is derived from a
persisted ``last_run`` plus the interval, compared against the wall clock on a
30-second tick. A Home Assistant restart, a host suspend, or a long upgrade
therefore cannot make the cadence drift or double-fire an interval that already
ran.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_GATE_ENTITY,
    CONF_GATE_STATE,
    CONF_INTERVAL_MINUTES,
    CONF_PAUSED,
    CONF_STARTUP_DELAY_SECONDS,
    CONF_WRITE_CSV,
    DOMAIN,
    OPTIONS,
    STORAGE_KEY,
    STORAGE_VERSION,
    TICK_SECONDS,
    clamp,
)
from .engine import SpeedResult, SpeedsterEngine, create_session
from .history import append as csv_append
from .history import last as csv_last
from .history import totals as csv_totals
from .report import build as build_report

_LOGGER = logging.getLogger(__name__)

type SpeedsterConfigEntry = ConfigEntry[SpeedsterCoordinator]


class SpeedsterCoordinator(DataUpdateCoordinator[SpeedResult | None]):
    """Owns the engine, the schedule, the CSV log and the running totals."""

    config_entry: SpeedsterConfigEntry

    def __init__(self, hass: HomeAssistant, entry: SpeedsterConfigEntry) -> None:
        """Set up the coordinator without starting anything yet."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=None,  # the schedule is driven by _tick, not the coordinator
        )
        self._store = Store[dict[str, Any]](hass, STORAGE_VERSION, STORAGE_KEY)
        self._session = create_session()
        self._engine = SpeedsterEngine(self._session)
        self.csv_path = Path(hass.config.path(DOMAIN)) / "results.csv"
        self.last_run: datetime | None = None
        self.total_bytes = 0
        self.test_count = 0
        self.testing = False
        self.last_error = ""
        self._not_before = dt_util.utcnow()
        self._gate_unsub: Any = None

    # ---- options ----

    @property
    def options(self) -> dict[str, Any]:
        """Every option, defaulted and clamped exactly as ``Settings.Clamp`` does."""
        stored = self.config_entry.options
        out: dict[str, Any] = {}
        for key, (default, low, high) in OPTIONS.items():
            out[key] = clamp(stored.get(key, default), low, high)
        return out

    @property
    def paused(self) -> bool:
        """Report whether the schedule is suspended."""
        return bool(self.options[CONF_PAUSED])

    @property
    def interval(self) -> timedelta:
        """Current cadence."""
        return timedelta(minutes=self.options[CONF_INTERVAL_MINUTES])

    @property
    def next_run(self) -> datetime | None:
        """When the next scheduled test is due, or None while paused."""
        if self.paused:
            return None
        if self.last_run is None:
            return max(self._not_before, dt_util.utcnow())
        return max(self.last_run + self.interval, self._not_before)

    async def async_set_option(self, key: str, value: Any) -> None:
        """Change one option, the way the tray menu writes one key at a time."""
        self.hass.config_entries.async_update_entry(
            self.config_entry, options={**self.config_entry.options, key: value}
        )

    # ---- lifecycle ----

    async def async_prepare(self) -> None:
        """Restore state, give the host a grace period, then start ticking."""
        stored = await self._store.async_load() or {}
        if raw := stored.get("last_run"):
            try:
                self.last_run = dt_util.parse_datetime(raw)
            except (TypeError, ValueError):
                self.last_run = None

        # Seed the entities from the log so a restart does not blank them out.
        last, totals = await self.hass.async_add_executor_job(self._read_log)
        self.total_bytes, self.test_count = totals
        self.async_set_updated_data(last)

        delay = self.options[CONF_STARTUP_DELAY_SECONDS]
        self._not_before = dt_util.utcnow() + timedelta(seconds=delay)

        self.config_entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._tick, timedelta(seconds=TICK_SECONDS), name="speedster"
            )
        )
        self.config_entry.async_on_unload(self._async_unsub_gate)
        self._async_watch_gate()

    def _read_log(self) -> tuple[SpeedResult | None, tuple[int, int]]:
        """Blocking: last successful result plus cumulative totals."""
        return csv_last(self.csv_path), csv_totals(self.csv_path)

    async def async_shutdown(self) -> None:
        """Close the measurement session."""
        await super().async_shutdown()
        await self._session.close()

    @callback
    def _async_unsub_gate(self) -> None:
        """Stop watching the gate entity."""
        if self._gate_unsub:
            self._gate_unsub()
            self._gate_unsub = None

    @callback
    def _async_watch_gate(self) -> None:
        """Re-check the schedule when the gate changes, as the app does on network change."""
        self._async_unsub_gate()
        if entity_id := self.options[CONF_GATE_ENTITY]:
            self._gate_unsub = async_track_state_change_event(
                self.hass, [entity_id], self._async_gate_changed
            )

    @callback
    def _async_gate_changed(self, _event: Event[EventStateChangedData]) -> None:
        """Run a due test right now if the gate transition allows it."""
        self.hass.async_create_task(self._tick(dt_util.utcnow()))

    @callback
    def async_options_updated(self) -> None:
        """Re-arm the gate listener after an options change."""
        self._async_watch_gate()

    # ---- schedule ----

    async def _tick(self, _now: datetime) -> None:
        """Fire a test when one is due. Called every 30 seconds."""
        if self.testing or self.paused:
            return
        now = dt_util.utcnow()
        if now < self._not_before:
            return
        if self.last_run is not None and now < self.last_run + self.interval:
            return
        await self.async_run_test(scheduled=True)

    def _blocked(self) -> str | None:
        """Why a scheduled test must not run, in the same order the app checks."""
        if self.paused:
            return "skipped: paused"
        entity_id = self.options[CONF_GATE_ENTITY]
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return f"skipped: gate {entity_id} unavailable"
        if state.state != self.options[CONF_GATE_STATE]:
            return f"skipped: gate {entity_id} is {state.state}"
        return None

    async def async_run_test(self, *, scheduled: bool) -> SpeedResult | None:
        """Run one test. A scheduled run respects the gate; a manual one does not."""
        if self.testing:
            return self.data

        if scheduled and (reason := self._blocked()):
            # A blocked scheduled attempt is logged as an explained gap and burns the
            # interval, so a closed gate cannot cause a retry storm.
            skip = SpeedResult(error=reason)
            await self._async_record(skip, burn_interval=True)
            _LOGGER.debug("scheduled test %s", reason)
            return skip

        self.testing = True
        self.async_update_listeners()
        try:
            result = await self._engine.run(self.options)
            await self._async_record(result, burn_interval=True)
            _LOGGER.debug(
                "result %.2f/%.2f Mbps over %.1f/%.1f s, %.1f ms, %s+%s bytes%s",
                result.down_mbps,
                result.up_mbps,
                result.down_seconds,
                result.up_seconds,
                result.latency_ms,
                result.down_bytes,
                result.up_bytes,
                "" if result.ok else f", error={result.error}",
            )
            return result
        finally:
            self.testing = False
            self.async_update_listeners()

    async def _async_record(self, result: SpeedResult, *, burn_interval: bool) -> None:
        """Append to the log, advance the schedule, and publish to the entities."""
        if self.options[CONF_WRITE_CSV]:
            await self.hass.async_add_executor_job(csv_append, self.csv_path, result)
        self.total_bytes += result.total_bytes
        self.test_count += 1
        if burn_interval:
            self.last_run = result.timestamp_utc
            await self._store.async_save({"last_run": self.last_run.isoformat()})
        # A skip or a failure must not overwrite the last good reading on the sensors.
        if result.ok:
            self.last_error = ""
            self.async_set_updated_data(result)
        else:
            self.last_error = result.error
            self.async_update_listeners()

    # ---- derived figures ----

    @property
    def projected_monthly_bytes(self) -> float:
        """The report's "Projected / month" tile: last test's cost at the current cadence."""
        if not self.data or not (minutes := self.options[CONF_INTERVAL_MINUTES]):
            return 0.0
        tests_per_month = 30 * 24 * 60 / minutes
        return self.data.total_bytes * tests_per_month

    async def async_build_report(self) -> Path:
        """Regenerate report.html under <config>/www/speedster/."""
        return await self.hass.async_add_executor_job(
            build_report, self.csv_path, Path(self.hass.config.config_dir), self.options
        )
