"""results.csv compatibility tests.

The CSV is the log of record and is shared in format with the Windows app, so a
history from either side must be readable by the other.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from custom_components.speedster import history
from custom_components.speedster.const import CSV_HEADER
from custom_components.speedster.engine import SpeedResult

if TYPE_CHECKING:
    from pathlib import Path

RESULT = SpeedResult(
    timestamp_utc=datetime(2026, 8, 24, 10, 30, tzinfo=UTC),
    down_mbps=8.234,
    up_mbps=2.5,
    latency_ms=23.4,
    jitter_ms=1.25,
    down_seconds=3.1,
    up_seconds=3.0,
    down_bytes=4_100_000,
    up_bytes=900_000,
    network="Home, Wi-Fi",
    server="London (LHR)",
)


def test_header_written_once(tmp_path: Path) -> None:
    """The header goes in ahead of the first row and never again."""
    path = tmp_path / "results.csv"
    history.append(path, RESULT)
    history.append(path, RESULT)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == CSV_HEADER
    assert len(lines) == 3


def test_round_trip(tmp_path: Path) -> None:
    """A row written here reads back with the same values, commas and all."""
    path = tmp_path / "results.csv"
    history.append(path, RESULT)
    restored = history.last(path)
    assert restored is not None
    assert restored.down_mbps == 8.234
    assert restored.network == "Home, Wi-Fi"
    assert restored.server == "London (LHR)"
    assert restored.down_bytes == 4_100_000
    assert restored.timestamp_utc == RESULT.timestamp_utc


def test_failed_row_leaves_metrics_empty(tmp_path: Path) -> None:
    """A skip is an explained gap: bytes are recorded, rates are blank."""
    path = tmp_path / "results.csv"
    history.append(path, SpeedResult(error="skipped: gate switch.vpn is on"))
    row = path.read_text(encoding="utf-8").splitlines()[1].split(",")
    assert row[1] == ""
    assert row[11] == "skipped: gate switch.vpn is on"
    assert history.last(path) is None


def test_legacy_rows_are_padded(tmp_path: Path) -> None:
    """Rows written before down_seconds/up_seconds existed still parse."""
    path = tmp_path / "results.csv"
    path.write_text(
        CSV_HEADER + "\n2026-01-01T00:00:00+00:00,9,3,20,1,1000,500,Home,no,cloudflare,LHR,\n",
        encoding="utf-8",
    )
    restored = history.last(path)
    assert restored is not None
    assert restored.down_seconds == 0.0


def test_totals(tmp_path: Path) -> None:
    """Cumulative cost counts every row, successful or not."""
    path = tmp_path / "results.csv"
    history.append(path, RESULT)
    history.append(path, SpeedResult(error="skipped: paused"))
    assert history.totals(path) == (5_000_000, 2)


def test_missing_file(tmp_path: Path) -> None:
    """No log yet is not an error."""
    path = tmp_path / "nothing.csv"
    assert history.read(path) == []
    assert history.last(path) is None
    assert history.totals(path) == (0, 0)
