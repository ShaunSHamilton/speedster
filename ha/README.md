# Speedster for Home Assistant

The measurement engine from [Speedster](../README.md), running inside Home Assistant instead of a Windows tray.

- **Download, upload, latency, jitter** measured against `speed.cloudflare.com`, with the same estimator as the desktop app: the rate is computed only over the window that survives dropping the TCP slow-start opening, so a short sample reads honest rather than low.
- **Bytes transferred** recorded per test, so the cost of the monitoring itself is a sensor you can chart and alert on. A default test costs about 4 - 5 MB, because it measures for a target number of seconds rather than pulling a fixed pile of bytes.
- **Drift-proof schedule** derived from a persisted `last_run`, so a restart, an upgrade or a suspended host cannot double-fire or skew the cadence.
- **Nothing leaves the machine** except the test itself. The log is a plain CSV under your config directory, in the same format as the desktop app - the two histories concatenate.

`Speedster.exe` is Windows-only (WinForms, GDI+, WinRT, registry) and cannot run on Home Assistant OS. This is an independent port of the parts that matter, not a wrapper.

## Install

**HACS** - add `https://github.com/ShaunSHamilton/speedster` as a custom repository of type *Integration*, install Speedster, restart Home Assistant, then **Settings -> Devices & services -> Add integration -> Speedster**.

**Manually** - copy [`custom_components/speedster`](../custom_components/speedster) into `<config>/custom_components/`, copy repo root `portal.html` into that same folder (it is the report template), and restart.

Requires Home Assistant 2026.1 or newer. No extra Python packages.

## Entities

| Entity | What it is |
| --- | --- |
| `sensor.speedster_download` / `_upload` | Mbps from the last test. The `measured_seconds` attribute is the window the rate came from - a short window is a less certain reading. |
| `sensor.speedster_latency` / `_jitter` | Minimum round trip with Cloudflare's own processing time subtracted, and the mean absolute difference between consecutive samples. |
| `sensor.speedster_last_test_data` | Bytes the last test spent, with `down_bytes` / `up_bytes` attributes. |
| `sensor.speedster_total_data_used` | Cumulative cost of every test in the log. `total_increasing`, so the energy-style cards and long-term statistics work. |
| `sensor.speedster_projected_monthly_data` | The report's *Projected / month* tile: the last test's cost at the current cadence. |
| `sensor.speedster_last_test` / `_next_test` | Timestamps. `next_test` is unavailable while paused. |
| `sensor.speedster_status` | `ok`, `testing`, `skipped`, `failed` or `unknown`, with `error` and `server` attributes. |
| `binary_sensor.speedster_problem` | On when the last run failed. A *skip* is an explained gap, not a problem, so it does not trip this. |
| `binary_sensor.speedster_testing` | On while a test is in flight. |
| `switch.speedster_pause` | Suspends the schedule. |
| `button.speedster_run_test_now` | Test immediately, off-schedule, ignoring the gate. |
| `button.speedster_build_report` | Regenerate `report.html`. |

## Services

`speedster.run_test` - one test now. `ignore_gate` defaults to true; set it false to respect the gate as a scheduled run would.

`speedster.build_report` - regenerates the report and returns `{path, url}`. Add it to a dashboard with a Webpage card pointing at `/local/speedster/report.html`.

## Options

Every knob from the desktop app's `settings.ini`, with the same names, defaults and clamped ranges - values outside the range are pulled back on load rather than rejected, because the bounds exist to stop a typo producing a hung or runaway test, not to second-guess a deliberate choice. See the [main README](../README.md#settingsini) for the full table and the data-cost discussion.

Two things differ from the desktop app:

- **Gating.** Windows metered detection and the Wi-Fi SSID allow-list have no Home Assistant OS counterpart, so they are replaced by one general mechanism: pick a **gate entity** and the **state** it must be in. A scheduled test is skipped while the gate is closed - a WAN-up binary sensor, a "not on the backup LTE link" template, a media-player-is-idle condition, whatever fits. A skipped scheduled run is logged with its reason and **burns the interval**, so a closed gate cannot cause a retry storm. Manual runs ignore the gate.
- **Engine.** Only the built-in Cloudflare engine is ported. The Ookla option shells out to a Windows `speedtest.exe`, which a custom integration cannot ship or install.

`paused` is the switch, not a form field. `last_run` is internal state, kept in `.storage/speedster`.

## Data

`<config>/speedster/results.csv` - one row per test, the same fourteen columns as the desktop app:

```
timestamp_utc,down_mbps,up_mbps,latency_ms,jitter_ms,down_bytes,up_bytes,network,metered,engine,server,error,down_seconds,up_seconds
```

Skipped and failed runs are logged too, with empty metrics and an `error` such as `skipped: gate binary_sensor.wan_ok is off`, so gaps in the chart are explained rather than silent. The recorder stores the sensor states as well, but the CSV keeps the columns the recorder has no place for - bytes, measurement window, skip reason - and is what the report is built from. Turn it off with the **Log every test to results.csv** option if you only want the sensors; the report then has nothing to draw.

`<config>/www/speedster/report.html` - written by **Build report**, served at `/local/speedster/report.html`. It is the same self-contained, offline report the desktop app generates, from the same `portal.html` template.

## Development

```bash
python -m venv ha/.venv
ha/.venv/bin/pip install -r ha/requirements-dev.txt
ha/.venv/bin/pytest -c ha/pyproject.toml  # engine, CSV and integration tests
ha/.venv/bin/python -m ha.tests.manual_run # one live test
ha/scripts/develop                         # throwaway Home Assistant instance
```

`tests/test_engine.py` is the one to keep green: it pins each branch of the rate estimator to a hand-computed answer, because a mistake there is invisible - the number still looks plausible.
