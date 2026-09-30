"""Transaction description normalization.

The same counterparty appears in many shapes inside bank statements:

    UPI/KALYAN/kalyan@okaxis/12345
    KALYAN KUMAR / UPI / kalyan@okaxis
    UPI-KALYAN-KALYAN@OKAXIS
    Transfer to Kalyan Kumar A/C XXXX1234
    NEFT-KALYAN KUMAR-XXXX1234
    PAYTM-KALYAN-XXXX1234
    PHONEPE-KALYAN@OKAXIS

This module extracts and normalizes every identifier it can find. The raw
text is never modified - results are returned as separate fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.models.enums import PaymentMethod
from app.security.masking import mask_account

# --- Regex library ---------------------------------------------------------

EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
# UPI handles have no dot in the domain (kalyan@okaxis, name@ybl, ...).
UPI_RE = re.compile(r"([A-Za-z0-9._-]{2,}@[A-Za-z]{2,})")
UPI_URL_RE = re.compile(r"upi://pay\?[^#\s]*?\bpa=([^&\s]+)", re.IGNORECASE)

ACCOUNT_RE = re.compile(
    r"(?:A/?C|A\.?C\.?|ACC(?:OUNT)?|ACCT)(?:\s*(?:NO|NUM|NUMBER|#))?\s*[:.\-#]?\s*"
    r"([Xx*]{2,}\s*\d{3,4}|\d{6,18})",
    re.IGNORECASE,
)
MASKED_ACCOUNT_RE = re.compile(r"\b([Xx*]{4,}\s*\d{4})\b")

REFERENCE_RE = re.compile(
    r"\b(?:UTR|REF(?:ERENCE)?(?:\s*(?:NO|NUM|NUMBER|#))?|RRN|TXN(?:\s*(?:ID|NO))?|"
    r"CHEQUE(?:\s*(?:NO|NUM))?|CHQ(?:\s*(?:NO|NUM))?|NEFT\s*NO|IMPS\s*NO|RTGS\s*NO)"
    r"\s*[:.#-]?\s*([A-Za-z0-9]{6,30})",
    re.IGNORECASE,
)

# Indian Banking (e.g. SBI, HDFC) structured UPI patterns:
# e.g. "UPI/DR/509571559259/KIRSHNA/YESB/paytmqr6b6/UPI"
STRUCTURED_UPI_RE = re.compile(
    r"\bUPI/(?:DR|CR)/(?P<ref>\d{8,18})/(?P<name>[^/]+)/(?P<bank>[A-Za-z0-9]+)/(?P<handle>[^/\s]+)",
    re.IGNORECASE,
)

PHONE_RE = re.compile(
    r"(?:\+91[\s-]*)?\b([6-9]\d{9})\b"
)
PHONE_CONTEXT_RE = re.compile(
    r"\b(?:MOB(?:ILE)?|PHONE|PH|TEL|CONTACT)\s*(?:NO\.?|NUMBER)?\s*[:.\-#]?\s*"
    r"(?:\+91[\s-]*)?([6-9]\d{9})",
    re.IGNORECASE,
)

DATE_IN_TEXT_RE = re.compile(
    r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b|\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b"
)

# Ordered: first match wins (app-specific tokens before generic "UPI").
_PAYMENT_METHOD_PATTERNS: list[tuple[re.Pattern[str], PaymentMethod]] = [
    (re.compile(r"\bGOOGLE\s*PAY\b|\bGPAY\b|\bG\s*PAY\b|\bTEZ\b", re.IGNORECASE), PaymentMethod.GOOGLE_PAY),
    (re.compile(r"\bPHONE\s*PE\b|\bPHONEPE\b", re.IGNORECASE), PaymentMethod.PHONEPE),
    (re.compile(r"\bPAY\s*TM\b|\bPAYTM\b", re.IGNORECASE), PaymentMethod.PAYTM),
    (re.compile(r"\bIMPS\b", re.IGNORECASE), PaymentMethod.IMPS),
    (re.compile(r"\bNEFT\b", re.IGNORECASE), PaymentMethod.NEFT),
    (re.compile(r"\bRTGS\b", re.IGNORECASE), PaymentMethod.RTGS),
    (re.compile(r"\bATM\b", re.IGNORECASE), PaymentMethod.ATM),
    (re.compile(r"\bUPI\b", re.IGNORECASE), PaymentMethod.UPI),
    (re.compile(r"\b(?:BANK\s*TRANSFER|FUND\s*TRANSFER|INB\b|NEFT/IMPS|IMPS/NEFT)", re.IGNORECASE), PaymentMethod.BANK_TRANSFER),
    (re.compile(r"\b(?:VISA|MASTERCARD|RUPAY|E\s*COMMERCE|POS\b)", re.IGNORECASE), PaymentMethod.CARD),
    (re.compile(r"\bCASH\b", re.IGNORECASE), PaymentMethod.CASH),
]

# Tokens that describe the transaction, never the person.
_STOPWORDS = {
    "upi", "gpay", "googlepay", "google", "pay", "phonepe", "paytm", "tm",
    "imps", "neft", "rtgs", "atm", "pos", "dr", "cr", "inr", "rs", "rs.",
    "to", "from", "for", "the", "a", "an", "of", "and", "or",
    "transfer", "transferred", "trf", "ft", "payment", "paid", "pmt",
    "cheque", "chq", "check", "ref", "reference", "utr", "rrn", "no", "nos",
    "num", "number", "via", "wdr", "wdl", "dep", "deposit", "withdrawal",
    "withdrawals", "credited", "debited", "credit", "debit", "balance",
    "a/c", "ac", "acc", "acct", "account", "ifsc", "upiref", "refno",
    "transaction", "txn", "ben", "beneficiary", "mob", "mobile", "ph",
    "phone", "tel", "self", "bill", "recharge", "purchase", "merchant",
    "service", "charges", "fee", "tax", "gst", "towards", "fwd", "revert",
    "reversed", "refund", "ltd", "pvt", "limited", "india", "ms", "m/s",
    "mr", "mrs", "dr.", "id", "kind", "att", "obl", "ser", "kindly",
    "instapay", "bhim", "collect", "push", "pull", "mandate", "emandate",
    "salary", "atm", "wlan", "pos", "inb", "mb", "neft-", "impa",
}

_SEPARATORS = re.compile(r"[/|:;,_\-–—]+")
_MULTI_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s@./-]")
_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")

# Leading segments of a UPI local part that are routing noise, not the name.
_UPI_PREFIX_STOPWORDS = {
    "upi", "paytm", "phonepe", "gpay", "googlepay", "bhim", "neft",
    "imps", "rtgs", "to", "from", "tm",
}
# Single letters that are artifacts of splitting (A/C, M/s), not initials.
_SINGLE_LETTER_STOP = {"a", "c", "m", "s"}
# Masked account fragments must never be mistaken for references.
_MASKED_TOKEN = re.compile(r"[Xx*]{2,}\d{3,4}")


@dataclass
class NormalizedTransaction:
    """All identifiers extracted from one raw description."""

    raw_description: str
    payment_method: PaymentMethod | None = None
    name: str | None = None
    normalized_name: str | None = None
    name_tokens: list[str] = field(default_factory=list)
    upi_id: str | None = None
    normalized_upi_id: str | None = None
    account_identifier: str | None = None
    reference: str | None = None
    phone: str | None = None
    email: str | None = None

    @property
    def has_strong_identifier(self) -> bool:
        """Strong enough to justify creating a new person if none exists."""
        return bool(self.normalized_upi_id or self.account_identifier)


def detect_payment_method(raw: str) -> PaymentMethod | None:
    if not raw:
        return None
    for pattern, method in _PAYMENT_METHOD_PATTERNS:
        if pattern.search(raw):
            return method
    if UPI_URL_RE.search(raw) or UPI_RE.search(raw):
        return PaymentMethod.UPI
    if re.search(r"\bTRANSFER\b", raw, re.IGNORECASE):
        return PaymentMethod.BANK_TRANSFER
    return None


def normalize_upi_id(raw: str) -> str | None:
    """Lower-case canonical form: KALYAN@OKAXIS -> kalyan@okaxis."""
    if not raw:
        return None
    value = raw.strip().lower().strip(".;,")
    value = value.lstrip(".")
    if "@" not in value:
        return None
    handle, _, domain = value.partition("@")
    handle = handle.strip(".-_")
    domain = domain.strip(".-_")
    if not handle or not domain:
        return None
    return f"{handle}@{domain}"


def _clean_upi_handle(handle: str) -> tuple[str, list[str]]:
    """Strip routing prefixes/duplicates from a UPI local part.

    'UPI-KALYAN-KALYAN@OKAXIS' -> ('kalyan@okaxis', ['UPI', 'KALYAN'])
    'ravi-kumar@okaxis'         -> ('ravi-kumar@okaxis', [])
    """
    local, _, domain = handle.partition("@")
    local = local.lstrip(". ")
    parts = [part for part in local.split("-") if part != ""]
    dropped: list[str] = []
    while len(parts) > 1 and parts[0].lower() in _UPI_PREFIX_STOPWORDS:
        dropped.append(parts.pop(0))
    while len(parts) >= 2 and parts[0].lower() == parts[1].lower():
        dropped.append(parts.pop(0))
    cleaned = "-".join(parts)
    if not cleaned or "@" not in f"{cleaned}@{domain}":
        return handle, dropped
    return f"{cleaned}@{domain}", dropped


def normalize_name(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = _PUNCT.sub(" ", raw.lower())
    cleaned = _MULTI_SPACE.sub(" ", cleaned).strip()
    return cleaned or None


def _split_tokens(text: str) -> list[str]:
    text = _SEPARATORS.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    tokens: list[str] = []
    for token in text.split():
        token = token.strip(".-_")
        if not token or token.lower() in _STOPWORDS:
            continue
        if token.isdigit():
            continue
        if len(token) == 1:
            if token.lower() in _SINGLE_LETTER_STOP:
                continue
            if not token.isalpha():
                continue
        tokens.append(token)
    return tokens


def _extract_reference(raw: str, upi_remainder: str) -> str | None:
    match = REFERENCE_RE.search(raw)
    if match:
        return match.group(1).strip()

    # Trailing numeric segment of a UPI string: UPI/KALYAN/kalyan@okaxis/12345
    if upi_remainder:
        for segment in reversed(upi_remainder.replace("-", "/").split("/")):
            segment = segment.strip()
            if segment.isdigit() and 5 <= len(segment) <= 20:
                return segment

    # Standalone long alphanumeric reference token.
    for token in _TOKEN_SPLIT.split(raw):
        if len(token) < 10:
            continue
        if not (any(ch.isdigit() for ch in token) and any(ch.isalpha() for ch in token)):
            continue
        if DATE_IN_TEXT_RE.fullmatch(token):
            continue
        if _MASKED_TOKEN.search(token):
            continue  # e.g. KUMAR-XXXX1234 is a name + account, not a reference
        if "@" in token:
            continue
        return token.strip(".-")
    return None


def normalize_description(raw: str | None) -> NormalizedTransaction:
    """Extract every identifier from a raw statement description."""
    raw = (raw or "").strip()
    result = NormalizedTransaction(raw_description=raw)
    if not raw:
        return result

    result.payment_method = detect_payment_method(raw)

    # Check structured Indian banking UPI pattern first (e.g. SBI, HDFC)
    clean_slashes = re.sub(r"\s*/\s*", "/", raw)
    clean_slashes = " ".join(clean_slashes.split())
    structured_match = STRUCTURED_UPI_RE.search(clean_slashes)
    if structured_match:
        ref = structured_match.group("ref")
        raw_name = structured_match.group("name").strip()
        bank = structured_match.group("bank").strip()
        handle = structured_match.group("handle").strip()

        result.reference = ref
        if not result.payment_method:
            result.payment_method = PaymentMethod.UPI
        if "gpay" in handle.lower():
            result.payment_method = PaymentMethod.GOOGLE_PAY
        elif "paytm" in handle.lower():
            result.payment_method = PaymentMethod.PAYTM
        elif "phonepe" in handle.lower():
            result.payment_method = PaymentMethod.PHONEPE

        if "@" in handle:
            result.upi_id = handle
            result.normalized_upi_id = normalize_upi_id(handle)
        elif bank:
            constructed = f"{handle}@{bank.lower()}"
            result.upi_id = constructed
            result.normalized_upi_id = normalize_upi_id(constructed)

        tokens = _split_tokens(raw_name)
        if tokens:
            result.name_tokens = tokens
            result.name = " ".join(t.title() for t in tokens[:4])
            result.normalized_name = normalize_name(result.name)

        return result

    working = raw

    # Email first so the UPI regex does not swallow the domain part.
    email_match = EMAIL_RE.search(working)
    if email_match:
        result.email = email_match.group(1).lower()
        working = working.replace(email_match.group(0), " ")

    upi_match = UPI_URL_RE.search(working) or UPI_RE.search(working)
    upi_remainder = ""
    if upi_match:
        matched_text = upi_match.group(0)
        handle, dropped = _clean_upi_handle(upi_match.group(1))
        result.upi_id = handle
        result.normalized_upi_id = normalize_upi_id(handle)
        upi_remainder = working[upi_match.end():]
        # Keep trimmed routing segments ("UPI-", "KALYAN-") as name candidates:
        # the raw description is never modified outside this local variable.
        prefix = " ".join(dropped)
        working = working.replace(matched_text, f"{prefix} " if prefix else " ")

    account_match = ACCOUNT_RE.search(working) or MASKED_ACCOUNT_RE.search(working)
    if account_match:
        result.account_identifier = mask_account(account_match.group(1))
        working = working.replace(account_match.group(0), " ")

    phone_match = PHONE_CONTEXT_RE.search(raw) or (
        PHONE_RE.search(raw) if re.search(r"\+91", raw) else None
    )
    if phone_match:
        result.phone = phone_match.group(1)
        working = working.replace(phone_match.group(0), " ")

    result.reference = _extract_reference(raw, upi_remainder)

    # Name: everything left after stripping known non-name content.
    name_source = working
    name_source = DATE_IN_TEXT_RE.sub(" ", name_source)
    tokens = _split_tokens(name_source)
    if not tokens and result.normalized_upi_id:
        # Derive a name candidate from the UPI handle itself, e.g.
        # PHONEPE-KALYAN@OKAXIS -> kalyan.
        local = result.normalized_upi_id.split("@", 1)[0]
        handle_tokens = [
            part.title()
            for part in re.split(r"[-._]", local)
            if part.isalpha() and len(part) >= 2
        ]
        tokens = handle_tokens
    if tokens:
        result.name_tokens = tokens
        result.name = " ".join(tokens[:6]).title()
        result.normalized_name = normalize_name(result.name)

    return result
