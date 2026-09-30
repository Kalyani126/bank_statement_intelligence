"""PII masking helpers.

Account numbers are masked everywhere they are displayed or logged; UPI ids
are only redacted in log output (they are needed in full for matching and are
often already partially masked in bank statements).
"""

from __future__ import annotations

import re

PASSWORD_PATTERNS = [
    re.compile(r"(password\s*[=:]\s*)\S+", re.IGNORECASE),
    re.compile(r"(pwd\s*[=:]\s*)\S+", re.IGNORECASE),
    re.compile(r"(typesafe_api_key\s*[=:]\s*)\S+", re.IGNORECASE),
    re.compile(r"(api[_-]?key\s*[=:]\s*)\S+", re.IGNORECASE),
    re.compile(r"(authorization:\s*bearer\s+)\S+", re.IGNORECASE),
]


def mask_account(value: str | None) -> str | None:
    """Reduce an account number to its canonical masked form: XXXX1234.

    Works for full numbers (987654321012 -> XXXX1234) and already-masked
    values (XXXX1234 -> XXXX1234).
    """
    if not value:
        return value
    stripped = value.strip()
    if not stripped:
        return stripped
    digits = re.sub(r"\D", "", stripped)
    if len(digits) < 5:
        # Nothing meaningful to hide (already masked, or too short).
        return stripped
    return "XXXX" + digits[-4:]


def redact_upi(upi: str | None) -> str | None:
    """Shorten a UPI handle for log output: kalyan@okaxis -> ka***@okaxis."""
    if not upi or "@" not in upi:
        return upi
    handle, _, domain = upi.partition("@")
    head = handle[:2]
    return f"{head}***@{domain}"


def redact_sensitive(text: str) -> str:
    """Scrub passwords/keys from any text destined for logs or API errors."""
    result = text
    for pattern in PASSWORD_PATTERNS:
        result = pattern.sub(r"\1[REDACTED]", result)
    return result
