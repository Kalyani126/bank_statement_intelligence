"""Flexible date parsing for bank statement rows."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from dateutil import parser as _dateutil_parser

_FORMATS = [
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%d-%B %Y",
    "%d %B %Y",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d-%m-%y",
    "%d/%m/%y",
    "%d.%m.%Y",
    "%d-%b-%y",
    "%d %b %y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%m/%d/%Y",  # US style last: day-first is far more common in statements
]

# Excel serial date epoch.
_EXCEL_EPOCH = date(1899, 12, 30)


def _from_excel_serial(value: float) -> date | None:
    if 20000 <= value <= 80000 and float(value).is_integer():
        return _EXCEL_EPOCH + timedelta(days=int(value))
    return None


def parse_date_any(value: Any) -> date | None:
    """Best-effort date parsing. Returns None instead of guessing badly."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _from_excel_serial(float(value))

    text = str(value).strip()
    if not text:
        return None

    # Pure numbers may be excel serials.
    try:
        numeric = float(text.replace(",", ""))
    except ValueError:
        numeric = None
    if numeric is not None:
        serial = _from_excel_serial(numeric)
        if serial is not None:
            return serial

    # Drop trailing time parts like "01-05-2024 14:33:12" for strptime attempts.
    head = text.split(" ")[0] if " " in text and text.count(" ") > 2 else text
    candidates = [text]
    if head != text:
        candidates.append(head)
    # "2024-05-01T00:00:00" style
    if "T" in text:
        candidates.insert(0, text.split("T")[0])

    for candidate in candidates:
        for fmt in _FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue

    try:
        parsed = _dateutil_parser.parse(text, dayfirst=True, fuzzy=False)
        return parsed.date()
    except (ValueError, OverflowError):
        return None
