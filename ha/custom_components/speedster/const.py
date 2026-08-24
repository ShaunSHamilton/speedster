"""Constants and option handling for Speedster.

Every knob here is the Home Assistant counterpart of a key in the Windows app's
``settings.ini``, with the same name, default and clamp bounds (see ``Settings``
and ``Settings.Clamp`` in ``Speedster.cs``). Keeping them identical is what makes
results from the two implementations comparable.
"""

from __future__ import annotations

from typing import Any, Final

DOMAIN: Final = "speedster"
CF_HOST: Final = "https://speed.cloudflare.com"
USER_AGENT: Final = "Speedster"
TICK_SECONDS: Final = 30  # scheduler resolution, matches Config.TICK_MS

STORAGE_KEY: Final = DOMAIN
STORAGE_VERSION: Final = 1

# Same 14 columns as the Windows app's results.csv, in the same order.
CSV_HEADER: Final = (
    "timestamp_utc,down_mbps,up_mbps,latency_ms,jitter_ms,down_bytes,up_bytes,"
    "network,metered,engine,server,error,down_seconds,up_seconds"
)

SERVICE_RUN_TEST: Final = "run_test"
SERVICE_BUILD_REPORT: Final = "build_report"
ATTR_IGNORE_GATE: Final = "ignore_gate"

# Option keys.
CONF_INTERVAL_MINUTES: Final = "interval_minutes"
CONF_PAUSED: Final = "paused"
CONF_TARGET_SECONDS_DOWN: Final = "target_seconds_down"
CONF_TARGET_SECONDS_UP: Final = "target_seconds_up"
CONF_MAX_BYTES_DOWN: Final = "max_bytes_down"
CONF_MAX_BYTES_UP: Final = "max_bytes_up"
CONF_STREAMS: Final = "streams"
CONF_MAX_TEST_SECONDS: Final = "max_test_seconds"
CONF_DISCARD_MS: Final = "discard_ms"
CONF_DISCARD_PERCENT: Final = "discard_percent"
CONF_MIN_WINDOW_MS: Final = "min_window_ms"
CONF_SAMPLE_MS: Final = "sample_ms"
CONF_LATENCY_SAMPLES: Final = "latency_samples"
CONF_READ_BUFFER_BYTES: Final = "read_buffer_bytes"
CONF_WRITE_CHUNK_BYTES: Final = "write_chunk_bytes"
CONF_REQUEST_BYTES_MAX: Final = "request_bytes_max"
CONF_RETRY_COUNT: Final = "retry_count"
CONF_RETRY_DELAY_MS: Final = "retry_delay_ms"
CONF_STARTUP_DELAY_SECONDS: Final = "startup_delay_seconds"
CONF_GATE_ENTITY: Final = "gate_entity"
CONF_GATE_STATE: Final = "gate_state"
CONF_WRITE_CSV: Final = "write_csv"

# key -> (default, low, high). A bound of None means the value is not numeric.
OPTIONS: Final[dict[str, tuple[Any, Any, Any]]] = {
    CONF_INTERVAL_MINUTES: (60, 1, 525600),
    CONF_PAUSED: (False, None, None),
    CONF_TARGET_SECONDS_DOWN: (3.0, 0.1, 3600.0),
    CONF_TARGET_SECONDS_UP: (3.0, 0.1, 3600.0),
    CONF_MAX_BYTES_DOWN: (6000000, 50000, 100000000000),
    CONF_MAX_BYTES_UP: (3000000, 50000, 100000000000),
    CONF_STREAMS: (4, 1, 64),
    CONF_MAX_TEST_SECONDS: (20, 1, 3600),
    CONF_DISCARD_MS: (500, 0, 600000),
    CONF_DISCARD_PERCENT: (25, 0, 90),
    CONF_MIN_WINDOW_MS: (1200, 0, 3600000),
    CONF_SAMPLE_MS: (100, 10, 60000),
    CONF_LATENCY_SAMPLES: (12, 1, 1000),
    CONF_READ_BUFFER_BYTES: (65536, 1024, 8388608),
    CONF_WRITE_CHUNK_BYTES: (65536, 1024, 8388608),
    CONF_REQUEST_BYTES_MAX: (25000000, 100000, 90000000),
    CONF_RETRY_COUNT: (2, 0, 10),
    CONF_RETRY_DELAY_MS: (1000, 0, 60000),
    CONF_STARTUP_DELAY_SECONDS: (60, 0, 86400),
    CONF_GATE_ENTITY: ("", None, None),
    CONF_GATE_STATE: ("on", None, None),
    CONF_WRITE_CSV: (True, None, None),
}

# Presets offered by the tray menu's Frequency submenu.
INTERVAL_PRESETS: Final = (5, 15, 30, 60, 120, 360, 720, 1440)


def clamp(value: Any, low: Any, high: Any) -> Any:
    """Port of ``Settings.Bound``: pull a value back inside its range."""
    if low is None or high is None:
        return value
    return low if value < low else (high if value > high else value)


def default_options() -> dict[str, Any]:
    """Every option at its documented default."""
    return {key: spec[0] for key, spec in OPTIONS.items()}
