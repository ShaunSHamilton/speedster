"""Cloudflare speed-test engine.

A full-fidelity port of ``SpeedTest`` in ``Speedster.cs`` onto asyncio/aiohttp.
The measurement semantics are the point of this file and are deliberately kept
identical to the Windows app:

* latency is the *minimum* round trip to a zero-byte response, with Cloudflare's
  own ``Server-Timing`` durations subtracted; jitter is the mean absolute
  difference between consecutive samples. A warm-up sample is excluded but still
  performed, so the transfers inherit a grown congestion window.
* Each direction stops at whichever of ``target_seconds_*`` (measured from the
  first byte) or ``max_bytes_*`` arrives first, with ``max_test_seconds`` as a
  hard guard against a stalled transfer.
* Every byte is counted as it moves, so ``down_bytes``/``up_bytes`` are the real
  cost of the test.
* The *rate* is computed only over the window that survives dropping
  ``max(discard_ms, discard_percent%)`` of the opening, because that opening is
  TCP slow start and reads low. Nothing is dropped when the whole window is
  shorter than ``min_window_ms`` - there is none to spare.

The Ookla engine is not ported: it shells out to a Windows ``speedtest.exe``,
which a custom integration cannot ship or install into Home Assistant OS.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp

from .const import CF_HOST, USER_AGENT

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

_LOGGER = logging.getLogger(__name__)

RX_DUR = re.compile(r"dur\s*=\s*([\d.]+)", re.IGNORECASE)

#: Statuses meaning "you asked for too much too soon", not "this is broken".
THROTTLED_STATUS = (403, 429, 503)

#: Floor on the halved request size during a throttle retry (Speedster.cs:748).
RETRY_MIN_ASK = 100000

_SOCKET_TIMEOUT = 30


@dataclass(slots=True)
class SpeedResult:
    """One test, mirroring ``Result`` in Speedster.cs."""

    timestamp_utc: datetime = field(default_factory=lambda: datetime.now(UTC))
    down_mbps: float = 0.0
    up_mbps: float = 0.0
    latency_ms: float = 0.0
    jitter_ms: float = 0.0
    down_seconds: float = 0.0
    up_seconds: float = 0.0
    down_bytes: int = 0
    up_bytes: int = 0
    network: str = ""
    metered: bool = False
    engine: str = "cloudflare"
    server: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        """True when the test produced usable numbers."""
        return not self.error

    @property
    def total_bytes(self) -> int:
        """What this test cost, both directions."""
        return self.down_bytes + self.up_bytes


class _AbortedError(Exception):
    """Raised from the upload body generator when the stop condition trips."""


class Meter:
    """Bytes over time for one direction.

    Tracks the total moved, when the wire went live, and a cumulative-bytes
    sample every ``sample_ms`` so a rate can be computed over any sub-window.
    Everything runs on one event loop, so the C# ``Interlocked``/``lock`` guards
    become plain attributes.
    """

    def __init__(self) -> None:
        """Start the clock."""
        self._t0 = time.monotonic()
        self.bytes = 0
        self.first_tick: int = -1
        self.last_tick: int = 0
        self.error: str | None = None
        self._samples: list[tuple[int, int]] = []

    @property
    def elapsed_ms(self) -> int:
        """Milliseconds since the meter was created."""
        return int((time.monotonic() - self._t0) * 1000)

    def fail(self, msg: str) -> None:
        """Record a stream failure. The first one wins - it is the one worth reporting."""
        if self.error is None:
            self.error = msg

    def add(self, n: int) -> None:
        """Count bytes that have moved."""
        self.bytes += n

    def mark_first_byte(self) -> None:
        """Note when the first byte crossed the wire."""
        if self.first_tick < 0:
            self.first_tick = self.elapsed_ms

    def mark_end(self) -> None:
        """Note that a stream has finished."""
        t = self.elapsed_ms
        self.last_tick = max(self.last_tick, t)

    def sample(self) -> None:
        """Append a cumulative-bytes sample at the current time."""
        self._samples.append((self.elapsed_ms, self.bytes))

    def done(self, target_ms: int, hard_ms: int) -> bool:
        """Report whether the target window elapsed, or the hard guard tripped."""
        t = self.elapsed_ms
        if t >= hard_ms:
            return True
        return self.first_tick >= 0 and t - self.first_tick >= target_ms

    def _bytes_at(self, ms: int) -> int:
        """Cumulative bytes as of ``ms``, from the sample series."""
        b = 0
        for at, total in self._samples:
            if at <= ms:
                b = total
            else:
                break
        return b

    def estimate(self, cfg: Mapping[str, Any]) -> tuple[float, float]:
        """Mean rate over the window left after dropping the slow-start opening.

        Returns ``(seconds, mbps)`` where ``seconds`` is the surviving window,
        not the length of the transfer.
        """
        first = max(self.first_tick, 0)
        last = self.last_tick
        span = last - first
        if span <= 0:
            return 0.0, 0.0

        cut = first
        if span >= cfg["min_window_ms"]:
            drop = max(cfg["discard_ms"], int(span * cfg["discard_percent"] / 100.0))
            if drop < span:
                cut = first + drop

        total = self.bytes - self._bytes_at(cut)
        secs = (last - cut) / 1000.0
        if total <= 0 or secs <= 0:
            # Nothing left after the cut - fall back to the whole window.
            total = self.bytes
            secs = span / 1000.0
        if total <= 0 or secs <= 0:
            return 0.0, 0.0
        return secs, total * 8.0 / secs / 1e6


def server_time_ms(headers: Mapping[str, str]) -> float:
    """Cloudflare's own processing time for a request.

    It is reported as ``Server-Timing`` metrics (``cfSpeedEdge``,
    ``cfSpeedWorker``); their sum subtracted from the round trip lands within
    about 1 ms of the TCP ``min_rtt`` the edge reports, so sum every ``dur=``
    rather than taking the first.
    """
    header = headers.get("Server-Timing")
    if not header:
        return 0.0
    total = 0.0
    for match in RX_DUR.finditer(header):
        try:
            total += float(match.group(1))
        except ValueError:
            continue
    return total


def server_name(headers: Mapping[str, str]) -> str:
    """Human-readable edge location from the response headers."""
    colo = headers.get("colo") or ""
    city = headers.get("city") or ""
    if colo and city:
        return f"{city} ({colo})"
    return colo or city


def _upload_buffer(size: int) -> bytes:
    """Build the payload pattern from ``Up`` in Speedster.cs."""
    return bytes((i * 31 + 7) & 0xFF for i in range(size))


class SpeedsterEngine:
    """Runs one test against ``speed.cloudflare.com``."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Hold the session used for every request of every test."""
        self._session = session

    async def run(self, cfg: Mapping[str, Any], network: str = "") -> SpeedResult:
        """Measure latency, then download, then upload."""
        result = SpeedResult(network=network)
        try:
            result.latency_ms, result.jitter_ms, result.server = await self._latency(cfg)
            # The latency phase leaves a warm, keep-alive connection in the pool, so the
            # transfers below start with a grown congestion window rather than from cold.
            (
                result.down_bytes,
                result.down_seconds,
                result.down_mbps,
                down_error,
            ) = await self._transfer(cfg, upload=False)
            (
                result.up_bytes,
                result.up_seconds,
                result.up_mbps,
                up_error,
            ) = await self._transfer(cfg, upload=True)
            # A direction that moved nothing is a failure, not a 0 Mbps reading.
            if down_error and up_error:
                result.error = f"download and upload failed: {down_error}"
            elif down_error:
                result.error = f"download failed: {down_error}"
            elif up_error:
                result.error = f"upload failed: {up_error}"
        except asyncio.CancelledError:
            raise
        except Exception as err:
            result.error = f"{type(err).__name__}: {err}"
            _LOGGER.debug("test failed", exc_info=True)
        return result

    # ---- latency ----

    async def _latency(self, cfg: Mapping[str, Any]) -> tuple[float, float, str]:
        """Round-trip time to a zero-byte response, minus Cloudflare's processing time."""
        samples: list[float] = []
        server = ""
        # i == 0 is a warm-up: it pays for DNS + TCP + TLS and would skew both min and jitter.
        for i in range(cfg["latency_samples"] + 1):
            try:
                start = time.perf_counter()
                async with self._session.get(f"{CF_HOST}/__down?bytes=0") as resp:
                    await resp.read()
                    rtt = (time.perf_counter() - start) * 1000.0 - server_time_ms(resp.headers)
                    if rtt > 0 and i > 0:
                        samples.append(rtt)
                    if not server:
                        server = server_name(resp.headers)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - one lost sample is not a failed test
                if i == 0:
                    _LOGGER.debug("latency sample failed: %s", err)

        if not samples:
            return 0.0, 0.0, server

        jitter = 0.0
        if len(samples) > 1:
            diffs = sum(abs(b - a) for a, b in itertools.pairwise(samples))
            jitter = diffs / (len(samples) - 1)
        return min(samples), jitter, server

    # ---- throughput ----

    async def _transfer(
        self, cfg: Mapping[str, Any], *, upload: bool
    ) -> tuple[int, float, float, str | None]:
        """Run ``streams`` concurrent transfers and report what they moved.

        Returns ``(bytes, seconds, mbps, error)``. ``bytes`` is everything that
        actually crossed the wire; ``seconds``/``mbps`` describe the window the
        rate was measured over, which is shorter than the transfer by the
        discarded ramp-up.
        """
        max_bytes = cfg["max_bytes_up" if upload else "max_bytes_down"]
        target_ms = int(cfg["target_seconds_up" if upload else "target_seconds_down"] * 1000)
        hard_ms = cfg["max_test_seconds"] * 1000
        streams: int = cfg["streams"]
        per_stream = max(1, max_bytes // streams)
        meter = Meter()
        buffer = _upload_buffer(cfg["write_chunk_bytes"]) if upload else b""

        sampler = asyncio.create_task(self._sample(meter, cfg["sample_ms"]))

        async def one_stream() -> None:
            try:
                if upload:
                    await self._up(
                        meter, cfg, buffer, per_stream, target_ms=target_ms, hard_ms=hard_ms
                    )
                else:
                    await self._down(meter, cfg, per_stream, target_ms, hard_ms)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - one dead stream must not kill the test
                meter.fail(str(err) or type(err).__name__)
                _LOGGER.debug("%s stream failed: %s", "upload" if upload else "download", err)
            meter.mark_end()

        try:
            # The hard guard is per direction; this is the outer backstop for a wedged socket.
            async with asyncio.timeout(min(hard_ms * 3, 600000) / 1000):
                await asyncio.gather(*(one_stream() for _ in range(streams)))
        except TimeoutError:
            meter.fail("transfer timed out")
            meter.mark_end()
        finally:
            sampler.cancel()

        meter.sample()
        seconds, mbps = meter.estimate(cfg)
        error = None if meter.bytes > 0 else (meter.error or "no bytes transferred")
        return meter.bytes, seconds, mbps, error

    @staticmethod
    async def _sample(meter: Meter, sample_ms: int) -> None:
        """Append a cumulative-bytes sample every ``sample_ms`` until cancelled."""
        while True:
            await asyncio.sleep(sample_ms / 1000)
            meter.sample()

    async def _down(
        self, meter: Meter, cfg: Mapping[str, Any], want: int, target_ms: int, hard_ms: int
    ) -> None:
        """One stream's share, as however many back-to-back requests it takes.

        A throttled request is retried at half the size, which also shrinks what
        the next attempt asks for.
        """
        left = want
        while left > 0 and not meter.done(target_ms, hard_ms):
            ask = min(left, cfg["request_bytes_max"])
            left -= ask
            attempt = 0
            while True:
                try:
                    await self._down_once(meter, cfg, ask, target_ms, hard_ms)
                    break
                except aiohttp.ClientResponseError as err:
                    if attempt >= cfg["retry_count"] or err.status not in THROTTLED_STATUS:
                        raise
                    _LOGGER.debug("download throttled at %s bytes, retrying smaller: %s", ask, err)
                    if cfg["retry_delay_ms"]:
                        await asyncio.sleep(cfg["retry_delay_ms"] / 1000)
                    ask = max(RETRY_MIN_ASK, ask // 2)
                    attempt += 1

    async def _down_once(
        self, meter: Meter, cfg: Mapping[str, Any], ask: int, target_ms: int, hard_ms: int
    ) -> None:
        """Pull one ``/__down`` response, counting bytes as they arrive."""
        async with self._session.get(f"{CF_HOST}/__down?bytes={ask}") as resp:
            resp.raise_for_status()
            async for chunk in resp.content.iter_chunked(cfg["read_buffer_bytes"]):
                meter.mark_first_byte()
                meter.add(len(chunk))
                if meter.done(target_ms, hard_ms):
                    break

    async def _up(
        self,
        meter: Meter,
        cfg: Mapping[str, Any],
        buffer: bytes,
        want: int,
        *,
        target_ms: int,
        hard_ms: int,
    ) -> None:
        """One stream's share of the upload, in ``request_bytes_max`` sized requests."""
        remaining = want
        while remaining > 0 and not meter.done(target_ms, hard_ms):
            ask = min(remaining, cfg["request_bytes_max"])
            remaining -= ask
            attempt = 0
            stop = False
            while True:
                try:
                    stop = await self._up_once(
                        meter, buffer, ask, target_ms=target_ms, hard_ms=hard_ms
                    )
                    break
                except aiohttp.ClientResponseError as err:
                    if attempt >= cfg["retry_count"] or err.status not in THROTTLED_STATUS:
                        raise
                    _LOGGER.debug("upload throttled at %s bytes, retrying smaller: %s", ask, err)
                    if cfg["retry_delay_ms"]:
                        await asyncio.sleep(cfg["retry_delay_ms"] / 1000)
                    ask = max(RETRY_MIN_ASK, ask // 2)
                    attempt += 1
            if stop:
                break

    async def _up_once(
        self,
        meter: Meter,
        buffer: bytes,
        ask: int,
        *,
        target_ms: int,
        hard_ms: int,
    ) -> bool:
        """Push ``ask`` bytes.

        Returns True when the stop condition tripped mid-request, so the caller
        stops asking. Aborting mid-body leaves the declared ``Content-Length``
        unsatisfied and the connection is dropped - the same trade the Windows
        app makes with ``HttpWebRequest.Abort``.
        """
        aborted = False

        async def body() -> AsyncIterator[bytes]:
            nonlocal aborted
            left = ask
            while left > 0:
                n = min(left, len(buffer))
                # The clock starts when we begin pushing, not after.
                meter.mark_first_byte()
                yield buffer[:n]
                meter.add(n)
                left -= n
                if meter.done(target_ms, hard_ms):
                    aborted = True
                    raise _AbortedError

        try:
            async with self._session.post(
                f"{CF_HOST}/__up",
                data=body(),
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(ask),
                },
            ) as resp:
                resp.raise_for_status()
                await resp.read()
        except _AbortedError:
            pass
        except aiohttp.ClientError:
            # An aborted body surfaces as a connection error; that is expected.
            if not aborted:
                raise
        return aborted


def create_session(connector_limit: int = 64) -> aiohttp.ClientSession:
    """Build a session tuned for measurement: no proxies, no cache, generous socket timeouts."""
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=connector_limit),
        timeout=aiohttp.ClientTimeout(
            total=None, sock_connect=_SOCKET_TIMEOUT, sock_read=_SOCKET_TIMEOUT
        ),
        headers={
            "User-Agent": USER_AGENT,
            "Cache-Control": "no-cache, no-store",
            "Pragma": "no-cache",
        },
        trust_env=False,
        auto_decompress=False,
    )
