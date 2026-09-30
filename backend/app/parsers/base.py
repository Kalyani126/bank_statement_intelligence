"""Parser contracts shared by every statement format."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Protocol


class ParserError(Exception):
    """Statement could not be parsed. Message is safe to show to the user."""


class EncryptedPdfError(ParserError):
    """PDF requires a password. The password itself is never stored/logged."""

    def __init__(self, message: str | None = None):
        super().__init__(
            message
            or "This PDF is password-protected. Provide the password to extract it."
        )


class ColumnDetectionError(ParserError):
    """No recognisable column layout was found in the file."""


@dataclass
class ParsedRow:
    transaction_date: date | None = None
    value_date: date | None = None
    description: str | None = None
    reference: str | None = None
    debit: Decimal | None = None
    credit: Decimal | None = None
    balance: Decimal | None = None
    raw_text: str = ""
    source_page: int | None = None


@dataclass
class ParsedStatement:
    rows: list[ParsedRow] = field(default_factory=list)
    bank_name: str | None = None
    account_holder: str | None = None
    account_number_raw: str | None = None
    warnings: list[str] = field(default_factory=list)
    # Balance carried into the period; lets reconciliation verify row 1.
    opening_balance: Decimal | None = None

    @property
    def period_start(self) -> date | None:
        dates = [r.transaction_date for r in self.rows if r.transaction_date]
        return min(dates) if dates else None

    @property
    def period_end(self) -> date | None:
        dates = [r.transaction_date for r in self.rows if r.transaction_date]
        return max(dates) if dates else None


class StatementParser(Protocol):
    def parse(
        self,
        data: bytes,
        *,
        filename: str = "",
        password: str | None = None,
    ) -> ParsedStatement: ...


# --- Bank sniffing ----------------------------------------------------------

BANK_SIGNATURES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"STATE BANK OF INDIA|\bSBI\b", re.I), "State Bank of India"),
    (re.compile(r"HDFC BANK|\bHDFC\b", re.I), "HDFC Bank"),
    (re.compile(r"ICICI BANK|\bICICI\b", re.I), "ICICI Bank"),
    (re.compile(r"AXIS BANK|\bAXIS\b", re.I), "Axis Bank"),
    (re.compile(r"KOTAK(?: MAHINDRA)? BANK|\bKOTAK\b", re.I), "Kotak Mahindra Bank"),
    (re.compile(r"PUNJAB NATIONAL BANK|\bPNB\b", re.I), "Punjab National Bank"),
    (re.compile(r"BANK OF BARODA|\bBOB\b", re.I), "Bank of Baroda"),
    (re.compile(r"CANARA BANK", re.I), "Canara Bank"),
    (re.compile(r"YES BANK", re.I), "Yes Bank"),
    (re.compile(r"IDFC(?: FIRST)? BANK|\bIDFC\b", re.I), "IDFC Bank"),
    (re.compile(r"INDUSIND BANK|\bINDUSIND\b", re.I), "IndusInd Bank"),
    (re.compile(r"AU SMALL FINANCE", re.I), "AU Small Finance Bank"),
    (re.compile(r"FEDERAL BANK", re.I), "Federal Bank"),
    (re.compile(r"BANDHAN BANK", re.I), "Bandhan Bank"),
    (re.compile(r"UNION BANK OF INDIA", re.I), "Union Bank of India"),
    (re.compile(r"CENTRAL BANK OF INDIA", re.I), "Central Bank of India"),
    (re.compile(r"INDIAN BANK", re.I), "Indian Bank"),
    (re.compile(r"UNITED BANK", re.I), "United Bank of India"),
]

_ACCOUNT_HEADER_RE = re.compile(
    r"(?:A/?C|Account)(?:\s*(?:No|Number|Num|#))?\s*[:.\-]?\s*"
    r"([Xx*]{2,}\s*\d{3,4}|\d{6,18})",
    re.IGNORECASE,
)
_HOLDER_RE = re.compile(
    r"(?:A/?C\s*(?:Holder|Name)|Name\s*of\s*(?:A/?C|Account\s*Holder)|"
    r"Account\s*Holder(?:'s)?\s*Name|Name)\s*[:.\-]\s*([A-Za-z][A-Za-z .'\-]{2,60})",
    re.IGNORECASE,
)


def sniff_bank_name(text: str) -> str | None:
    for pattern, name in BANK_SIGNATURES:
        if pattern.search(text or ""):
            return name
    return None


_OPENING_BALANCE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"opening\s*balance.{0,40}?([\d,]+\.\d{2})", re.IGNORECASE | re.S
    ),
    re.compile(
        r"previous\s*balance.{0,40}?([\d,]+\.\d{2})", re.IGNORECASE | re.S
    ),
    re.compile(
        r"balance\s*(?:b/?f|brought\s*forward).{0,40}?([\d,]+\.\d{2})",
        re.IGNORECASE | re.S,
    ),
)


def sniff_opening_balance(text: str) -> Decimal | None:
    """Balance carried into the period, so row 1 can be verified too."""
    for pattern in _OPENING_BALANCE_RES:
        match = pattern.search(text or "")
        if match:
            try:
                return Decimal(match.group(1).replace(",", "")).quantize(
                    Decimal("0.01")
                )
            except InvalidOperation:
                return None
    return None


def sniff_account_details(text: str) -> tuple[str | None, str | None]:
    """Return (holder_name, masked_account) found in statement headers."""
    holder = None
    account = None
    match = _HOLDER_RE.search(text or "")
    if match:
        holder = match.group(1).strip(" .,-")
    match = _ACCOUNT_HEADER_RE.search(text or "")
    if match:
        raw = match.group(1)
        digits = re.sub(r"\D", "", raw)
        if len(digits) >= 5:
            account = "XXXX" + digits[-4:]
    return holder, account
