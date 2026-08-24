"""The results.csv log, byte-for-byte compatible with the Windows app.

Port of ``Log`` in ``Speedster.cs``. Home Assistant's recorder already stores the
sensor states, but it does not store the columns that make Speedster Speedster -
bytes moved, the measurement window each rate came from, the skip reason for a
gap. So the CSV stays the log of record, which also means a history from the
Windows app and a history from here can simply be concatenated.

Everything in this module is blocking file I/O; call it from an executor.
"""

from __future__ import annotations

import csv
import logging
from datetime import UTC, datetime
from pathlib import Path

from .const import CSV_HEADER
from .engine import SpeedResult

_LOGGER = logging.getLogger(__name__)

FIELDS = 14


def _num(value: float, ok: bool) -> str:
    """Format a metric. A failed test writes an empty cell, never a misleading 0."""
    if not ok and value <= 0:
        return ""
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def append(path: Path, result: SpeedResult) -> None:
    """Append one row, writing the header first if the file is new or empty."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists() or path.stat().st_size == 0
        ok = result.ok
        row = [
            result.timestamp_utc.isoformat(),
            _num(result.down_mbps, ok),
            _num(result.up_mbps, ok),
            _num(result.latency_ms, ok),
            _num(result.jitter_ms, ok),
            str(result.down_bytes),
            str(result.up_bytes),
            result.network,
            "yes" if result.metered else "no",
            result.engine,
            result.server,
            result.error,
            _num(result.down_seconds, ok),
            _num(result.up_seconds, ok),
        ]
        with path.open("a", encoding="utf-8", newline="") as handle:
            if fresh:
                handle.write(CSV_HEADER + "\n")
            csv.writer(handle, lineterminator="\n").writerow(row)
    except OSError as err:
        _LOGGER.warning("csv append failed: %s", err)


def read(path: Path) -> list[list[str]]:
    """Rows as raw field lists, header skipped. Short legacy rows are padded to 14."""
    rows: list[list[str]] = []
    try:
        if not path.exists():
            return rows
        with path.open(encoding="utf-8", newline="") as handle:
            for fields in csv.reader(handle):
                if not fields or not any(fields):
                    continue
                if fields[0].lower().startswith("timestamp_utc"):
                    continue
                # Rows written before down_seconds/up_seconds existed.
                rows.append([*fields, *([""] * (FIELDS - len(fields)))][:FIELDS])
    except (OSError, csv.Error) as err:
        _LOGGER.warning("csv read failed: %s", err)
    return rows


def _float(value: str) -> float:
    """Parse a cell, treating anything unparseable as zero."""
    try:
        return float(value)
    except ValueError:
        return 0.0


def last(path: Path) -> SpeedResult | None:
    """Most recent successful result, used to give the entities a state at startup."""
    for fields in reversed(read(path)):
        if fields[11]:
            continue
        try:
            stamp = datetime.fromisoformat(fields[0])
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return SpeedResult(
            timestamp_utc=stamp,
            down_mbps=_float(fields[1]),
            up_mbps=_float(fields[2]),
            latency_ms=_float(fields[3]),
            jitter_ms=_float(fields[4]),
            down_bytes=int(_float(fields[5])),
            up_bytes=int(_float(fields[6])),
            network=fields[7],
            metered=fields[8] == "yes",
            engine=fields[9] or "cloudflare",
            server=fields[10],
            down_seconds=_float(fields[12]),
            up_seconds=_float(fields[13]),
        )
    return None


def totals(path: Path) -> tuple[int, int]:
    """``(total_bytes, test_count)`` over the whole log, for the cumulative sensors."""
    total = 0
    count = 0
    for fields in read(path):
        total += int(_float(fields[5])) + int(_float(fields[6]))
        count += 1
    return total, count
