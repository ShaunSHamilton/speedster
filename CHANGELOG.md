# Changelog

This project has no version numbers. Each entry is keyed by the **git commit SHA** it describes,
newest first. SHAs link to the commit on GitHub.

## Release process

1. **Make** the change.
2. **Build**: `.\build.ps1` (commit the refreshed `Speedster.exe` alongside the source).
3. **Commit** the change - this mints the SHA.
4. **Update** this changelog: add an entry under the new SHA, then commit it.

Because an entry references the SHA of an already-made commit, the changelog is updated *after* the
commit it documents. Changelog-only commits are not themselves listed.

---

## `TBD` - measure by time, not by bytes

*Add the SHA of this commit here once it exists.*

- A test now costs about 4 - 5 MB instead of 21 - 26 MB, and reads *more* accurately.
  `target_bytes_down`/`target_bytes_up` are replaced by a dual stop condition: each direction
  ends at `target_seconds_down`/`target_seconds_up` (default 3 s) or `max_bytes_down`/
  `max_bytes_up` (default 6 MB / 3 MB), whichever arrives first. The old keys are still read as
  the new `max_bytes_*`.
- Rates are computed over the window left after dropping `max(discard_ms, discard_percent)` of
  the opening, which is TCP slow start. Measured on a 9 Mbps line, a 1 MB and an 8 MB sample of
  one transfer agree within 2%, while the full 12 MB average read 20% low because it included a
  mid-transfer stall.
- New `down_seconds`/`up_seconds` columns record the window each rate was measured over; the
  report shows them on hover. Rows written before this change read as empty.
- A direction that transferred nothing is now logged as `error=download failed: ...` instead of
  `0 Mbps` with no error.
- Throttling handled: the test server answers 403/429 once a client has pulled a lot in a short
  window, so a throttled request is retried `retry_count` times at half the size, and one stream's
  share is split into requests of at most `request_bytes_max`.
- Everything is configurable with a wide clamped range: `streams` (1 - 64), `sample_ms`,
  `min_window_ms`, `latency_samples`, `read_buffer_bytes`, `write_chunk_bytes`, `retry_delay_ms`.

## `TBD` - init

*Add the SHA of the initial commit here once it exists.*

- Initial system-tray app: runs an internet speed test on a schedule and logs every result to
  `%LOCALAPPDATA%\Speedster\results.csv` in plain text.
- Built-in engine measures download, upload, latency and jitter against `speed.cloudflare.com`
  over `streams` concurrent connections, counting the bytes actually transferred; optional Ookla
  `speedtest.exe` engine selected by `engine`/`ookla_path`.
- Schedule is a 30-second timer against `last_run + interval_minutes` in `settings.ini`, so it
  survives sleep and restarts; re-checked on resume and on network change.
- Guards: **Pause**, **Skip metered networks** (WinRT `ConnectionCost` by reflection) and an
  `only_networks` allow-list keyed on the Network List Manager network name (the Wi-Fi SSID).
  Skipped runs are logged with an `error=skipped: ...` reason.
- **Open report** renders `report.html` from the embedded `portal.html` template: stat tiles,
  throughput and latency charts, cumulative bytes and a monthly projection, plus a table of
  recent tests. Self-contained and offline.
- Tray menu, single-instance mutex, and optional start-with-Windows (HKCU Run key).
- Built with the in-box .NET Framework 4.x `csc.exe` - no SDK required.
