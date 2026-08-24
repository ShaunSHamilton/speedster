"""Live smoke test, outside Home Assistant.

Runs one real test against speed.cloudflare.com with the documented defaults and
prints the result, so it can be compared against a same-minute **Run test now**
from the Windows tray app on the same link. A large gap between the two means the
rate estimator was ported wrong; a few percent is normal.

    python -m tests.manual_run            # from the ha/ directory
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.speedster.const import default_options  # noqa: E402
from custom_components.speedster.engine import (  # noqa: E402
    SpeedsterEngine,
    create_session,
)


async def main() -> int:
    """Run one test and print it."""
    options = default_options()
    session = create_session()
    try:
        result = await SpeedsterEngine(session).run(options)
    finally:
        await session.close()

    print(f"down     {result.down_mbps:8.2f} Mbps over {result.down_seconds:.2f} s")
    print(f"up       {result.up_mbps:8.2f} Mbps over {result.up_seconds:.2f} s")
    print(f"latency  {result.latency_ms:8.2f} ms   jitter {result.jitter_ms:.2f} ms")
    print(f"bytes    {result.down_bytes:,} down + {result.up_bytes:,} up")
    print(f"server   {result.server or '(unknown)'}")
    if result.error:
        print(f"error    {result.error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
