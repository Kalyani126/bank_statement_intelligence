"""Amount parsing helpers.

Handles ₹/Rs/commas, Dr/Cr suffixes and parenthesised negatives while
returning exact Decimal values - no floats for stored money.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

_CURRENCY = re.compile(r"(?:₹|rs\.?|inr)\s*", re.IGNORECASE)
_SIDE_SUFFIX = re.compile(r"\s*\b(dr|cr|dr\.|cr\.|debit|credit)\b\.?\s*$", re.IGNORECASE)
_SPACES = re.compile(r"[\s\u00a0]+")


def parse_amount(value: Any) -> Decimal | None:
    """Parse a monetary value; returns None when unparseable."""
    if value is None or value is True or value is False:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return None

    text = str(value).strip()
    if not text or text in {"-", "--", "N/A", "n/a"}:
        return None

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    text = _SIDE_SUFFIX.sub("", text).strip()
    text = _CURRENCY.sub("", text)
    text = text.replace(",", "").replace(" ", "")

    # Explicit trailing sign styles: 1000.00- or -1000.00
    if text.endswith("-"):
        negative = True
        text = text[:-1]
    if text.startswith("+"):
        text = text[1:]
    if text.startswith("-"):
        negative = True
        text = text[1:]

    if not text:
        return None

    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if negative:
        amount = -amount
    return amount.quantize(Decimal("0.01"))


def parse_amount_with_side(value: Any) -> tuple[Decimal | None, str | None]:
    """Return (amount, side) where side is 'DEBIT' | 'CREDIT' | None.

    The side is only reported when the source text explicitly says Dr/Cr.
    """
    if value is None:
        return None, None
    if isinstance(value, str):
        match = _SIDE_SUFFIX.search(value.strip())
        if match:
            token = match.group(1).lower()[0]
            side = "DEBIT" if token == "d" else "CREDIT"
            return parse_amount(value), side
    return parse_amount(value), None
