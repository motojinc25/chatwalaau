"""Raw ledger export as CSV (CTR-0201, PRP-0173, UDR-0155 D5).

One row per ledger record, in a FIXED column order that only ever grows at the end,
so a BI model built on one release keeps working on the next.

Every column is the stored ledger field as written, with two exceptions that are the
whole of this module's editorial licence:

* ``ts_local`` -- the single derived column: ``ts`` converted to the requested zone,
  so a BI tool can group on the same calendar the dashboard showed.
* The formula-injection guard -- a TEXT cell that a spreadsheet would evaluate
  (leading ``=``, ``+``, ``-``, ``@``, TAB or CR) is prefixed with ``'``. Numeric and
  timestamp columns are never touched.

An absent value is an EMPTY cell, never ``0`` (UDR-0135 D7). There is no price, cost
or currency column (UDR-0136 D3).
"""

from __future__ import annotations

import csv
from datetime import UTC, tzinfo
import io
from typing import TYPE_CHECKING, Any

from app.usage.aggregate import LOCAL_TS_KEY, record_datetime
from app.usage.ledger import TOKEN_FIELDS

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

# APPEND-ONLY. A future ledger field goes at the END, never in between (UDR-0155 D5).
# Pinned by tests/invariants/test_prp0173_usage_dashboard.py.
EXPORT_COLUMNS: tuple[str, ...] = (
    "ts",
    "ts_local",
    "lane",
    "kind",
    "purpose",
    "thread_id",
    "temporary",
    "model",
    "provider",
    "run_target",
    "node",
    "agent",
    "run_id",
    "model_calls",
    *TOKEN_FIELDS,
    "outcome",
)

# Columns whose values are numbers, booleans or timestamps -- never neutralized.
_NON_TEXT_COLUMNS = frozenset({"ts", "ts_local", "temporary", "model_calls", *TOKEN_FIELDS})

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# UTF-8 BOM: without it Excel reads non-ASCII run target / agent names as mojibake.
BOM = chr(0xFEFF)


def _cell(column: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if column not in _NON_TEXT_COLUMNS and text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def export_row(record: dict[str, Any], tz: tzinfo = UTC) -> list[str]:
    """Render one record as the ordered cells of ``EXPORT_COLUMNS``."""
    cells: list[str] = []
    for column in EXPORT_COLUMNS:
        if column == "ts_local":
            when = record.get(LOCAL_TS_KEY) or record_datetime(record, tz)
            cells.append(when.isoformat() if when else "")
        else:
            cells.append(_cell(column, record.get(column)))
    return cells


def iter_csv(records: Iterable[dict[str, Any]], tz: tzinfo = UTC) -> Iterator[str]:
    """Yield the CSV document chunk by chunk: BOM + header, then one row per record."""
    buffer = io.StringIO()
    # RFC 4180 line ends; the csv module handles quoting.
    writer = csv.writer(buffer, lineterminator="\r\n")

    def flush() -> str:
        chunk = buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        return chunk

    writer.writerow(EXPORT_COLUMNS)
    yield BOM + flush()
    for record in records:
        writer.writerow(export_row(record, tz))
        yield flush()


__all__ = ["BOM", "EXPORT_COLUMNS", "export_row", "iter_csv"]
