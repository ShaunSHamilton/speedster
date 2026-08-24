"""Engine tests.

The rate estimator is the part worth guarding: it is what makes a Speedster
number different from a naive bytes-over-total-time figure, and a mistake in it
is invisible - the result still looks plausible. Each case below pins one branch
of ``Meter.estimate`` to a hand-computed answer.
"""

from __future__ import annotations

from typing import Any

import pytest
from aioresponses import aioresponses

from custom_components.speedster.engine import (
    Meter,
    SpeedsterEngine,
    server_name,
    server_time_ms,
)

CFG: dict[str, Any] = {
    "discard_ms": 500,
    "discard_percent": 25,
    "min_window_ms": 1200,
}


def _meter(samples: list[tuple[int, int]], first: int, last: int) -> Meter:
    """A meter with a fixed sample series, so estimate() is deterministic."""
    meter = Meter()
    meter.first_tick = first
    meter.last_tick = last
    meter.bytes = samples[-1][1]
    meter._samples = samples
    return meter


def test_estimate_discard_ms_wins() -> None:
    """A short window drops discard_ms, because it is larger than the percentage."""
    # span 1600 ms, 25% = 400 ms < 500 ms, so the cut is at 500 ms.
    meter = _meter([(0, 0), (500, 1_000_000), (1600, 3_000_000)], first=0, last=1600)
    seconds, mbps = meter.estimate(CFG)
    assert seconds == pytest.approx(1.1)
    assert mbps == pytest.approx(2_000_000 * 8 / 1.1 / 1e6)


def test_estimate_discard_percent_wins() -> None:
    """A long window drops the percentage, because it is larger than discard_ms."""
    # span 4000 ms, 25% = 1000 ms > 500 ms, so the cut is at 1000 ms.
    meter = _meter([(0, 0), (1000, 1_000_000), (4000, 9_000_000)], first=0, last=4000)
    seconds, mbps = meter.estimate(CFG)
    assert seconds == pytest.approx(3.0)
    assert mbps == pytest.approx(8_000_000 * 8 / 3.0 / 1e6)


def test_estimate_min_window_suppresses_discard() -> None:
    """Below min_window_ms nothing is dropped - there is no window to spare."""
    meter = _meter([(0, 0), (500, 200_000), (1000, 500_000)], first=0, last=1000)
    seconds, mbps = meter.estimate(CFG)
    assert seconds == pytest.approx(1.0)
    assert mbps == pytest.approx(500_000 * 8 / 1.0 / 1e6)


def test_estimate_offsets_from_first_byte() -> None:
    """The window starts at the first byte, not at the start of the request."""
    # first byte at 200 ms, span 1200 ms, drop = max(500, 300) = 500, cut at 700 ms.
    meter = _meter([(0, 0), (700, 1_000_000), (1400, 4_000_000)], first=200, last=1400)
    seconds, mbps = meter.estimate(CFG)
    assert seconds == pytest.approx(0.7)
    assert mbps == pytest.approx(3_000_000 * 8 / 0.7 / 1e6)


def test_estimate_falls_back_when_cut_leaves_nothing() -> None:
    """When the cut consumes every byte, the whole window is used instead."""
    cfg = {**CFG, "min_window_ms": 0}
    meter = _meter([(0, 0), (500, 1_000_000)], first=0, last=600)
    seconds, mbps = meter.estimate(cfg)
    assert seconds == pytest.approx(0.6)
    assert mbps == pytest.approx(1_000_000 * 8 / 0.6 / 1e6)


def test_estimate_no_transfer() -> None:
    """A transfer that never started reports nothing rather than dividing by zero."""
    meter = Meter()
    assert meter.estimate(CFG) == (0.0, 0.0)


def test_done_dual_stop() -> None:
    """The hard guard trips regardless of the first byte; the target needs one."""
    meter = Meter()
    assert not meter.done(target_ms=10_000, hard_ms=20_000)
    meter.first_tick = 0
    meter.last_tick = 0
    # elapsed_ms is real time here, so assert the branch that does not need it.
    assert meter.done(target_ms=10_000, hard_ms=0)


def test_server_time_sums_every_duration() -> None:
    """cfSpeedEdge and cfSpeedWorker both count towards Cloudflare's own time."""
    header = {"Server-Timing": "cfSpeedEdge;dur=1.5, cfSpeedWorker;dur=2"}
    assert server_time_ms(header) == pytest.approx(3.5)
    assert server_time_ms({}) == 0.0


def test_server_name() -> None:
    """The edge is reported as city plus colo when both headers are present."""
    assert server_name({"colo": "LHR", "city": "London"}) == "London (LHR)"
    assert server_name({"colo": "LHR"}) == "LHR"
    assert server_name({}) == ""


@pytest.mark.asyncio
async def test_download_counts_bytes_and_stops_at_ceiling(options: dict[str, Any]) -> None:
    """One stream, one request: every byte is counted and the ceiling is respected."""
    from custom_components.speedster.engine import create_session

    payload = b"x" * 400000
    session = create_session()
    try:
        with aioresponses() as mocked:
            mocked.get(
                "https://speed.cloudflare.com/__down?bytes=400000",
                status=200,
                body=payload,
            )
            engine = SpeedsterEngine(session)
            moved, seconds, mbps, error = await engine._transfer(options, upload=False)
    finally:
        await session.close()

    assert error is None
    assert moved == len(payload)
    assert seconds >= 0
    assert mbps >= 0


@pytest.mark.asyncio
async def test_download_retries_smaller_when_throttled(options: dict[str, Any]) -> None:
    """A 403 is a "too much too soon", so the same stream asks for half."""
    from custom_components.speedster.engine import create_session

    session = create_session()
    try:
        with aioresponses() as mocked:
            mocked.get("https://speed.cloudflare.com/__down?bytes=400000", status=403)
            mocked.get(
                "https://speed.cloudflare.com/__down?bytes=200000",
                status=200,
                body=b"y" * 200000,
            )
            engine = SpeedsterEngine(session)
            moved, _seconds, _mbps, error = await engine._transfer(options, upload=False)
    finally:
        await session.close()

    assert error is None
    assert moved == 200000


@pytest.mark.asyncio
async def test_direction_that_moved_nothing_is_an_error(options: dict[str, Any]) -> None:
    """Zero bytes is a failure, never a 0 Mbps reading."""
    from custom_components.speedster.engine import create_session

    session = create_session()
    try:
        with aioresponses() as mocked:
            mocked.get(
                "https://speed.cloudflare.com/__down?bytes=400000",
                status=500,
                repeat=True,
            )
            engine = SpeedsterEngine(session)
            moved, _seconds, mbps, error = await engine._transfer(options, upload=False)
    finally:
        await session.close()

    assert moved == 0
    assert mbps == 0.0
    assert error is not None
