"""report.html generation.

Port of ``Portal`` in ``Speedster.cs``. The template is the repo's own
``portal.html`` - one source of truth for the report UI, shared with the Windows
app, which embeds the same file as a managed resource. This module only does the
substitution and the JS-safe escaping.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

from .const import CONF_INTERVAL_MINUTES, CONF_PAUSED
from .history import read as csv_read

_LOGGER = logging.getLogger(__name__)

TEMPLATE_NAME = "portal.html"
#: Written under <config>/www so Home Assistant serves it at /local/speedster/report.html.
OUTPUT_RELATIVE = Path("www") / "speedster" / "report.html"

_DATA_MARKER = "/*__DATA__*/[]"
_META_MARKER = "/*__META__*/{}"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class TemplateMissingError(Exception):
    """The portal.html template could not be found."""


def _template_path() -> Path:
    """Find portal.html: shipped beside the package, else the source tree above it."""
    here = Path(__file__).parent
    for candidate in (
        here / TEMPLATE_NAME,
        here.parents[1] / TEMPLATE_NAME,  # repo root, when running from a checkout
    ):
        if candidate.is_file():
            return candidate
    msg = f"{TEMPLATE_NAME} not found next to the integration or in the repo root"
    raise TemplateMissingError(msg)


def _js_str(value: str | None) -> str:
    r"""Quote a string for embedding in a script block.

    ``<`` becomes ``\\u003c`` so a value can never close the script element.
    """
    out = ['"']
    for char in value or "":
        if char in ('"', "\\"):
            out.append("\\" + char)
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "<":
            out.append("\\u003c")
        elif char < " ":
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _js_num(value: str) -> str:
    """Format a numeric cell, or ``null`` when the cell is empty or unparseable."""
    try:
        number = float(value)
    except ValueError:
        return "null"
    if not math.isfinite(number):
        return "null"
    return f"{number:.6f}".rstrip("0").rstrip(".") or "0"


def _epoch_ms(iso: str) -> str:
    """ISO-8601 to epoch milliseconds, as the chart expects."""
    try:
        stamp = datetime.fromisoformat(iso)
    except ValueError:
        return "0"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return str(int((stamp - _EPOCH).total_seconds() * 1000))


def build(csv_path: Path, config_dir: Path, options: Mapping[str, Any]) -> Path:
    """Write report.html from the CSV and return where it landed.

    Blocking - call it from an executor.
    """
    template = _template_path().read_text(encoding="utf-8")

    rows = [
        "["
        + ",".join(
            (
                _epoch_ms(f[0]),
                _js_num(f[1]),
                _js_num(f[2]),
                _js_num(f[3]),
                _js_num(f[4]),
                _js_num(f[5]),
                _js_num(f[6]),
                _js_str(f[7]),
                "1" if f[8] == "yes" else "0",
                _js_str(f[9]),
                _js_str(f[10]),
                _js_str(f[11]),
                _js_num(f[12]),
                _js_num(f[13]),
            )
        )
        + "]"
        for f in csv_read(csv_path)
    ]
    data = "[" + ",\n".join(rows) + "]"

    meta = (
        "{interval:"
        + str(int(options[CONF_INTERVAL_MINUTES]))
        + ",paused:"
        + ("true" if options[CONF_PAUSED] else "false")
        + ",engine:"
        + _js_str("cloudflare")
        + ",generated:"
        + _js_str(datetime.now().strftime("%Y-%m-%d %H:%M"))  # noqa: DTZ005 - local, display only
        + "}"
    )

    html = template.replace(_DATA_MARKER, data).replace(_META_MARKER, meta)
    out = config_dir / OUTPUT_RELATIVE
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    _LOGGER.debug("wrote %s (%s rows)", out, len(rows))
    return out
