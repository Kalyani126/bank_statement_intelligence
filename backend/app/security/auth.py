"""Password hashing and stateless bearer tokens.

Uses only the standard library (pbkdf2_hmac + HMAC-SHA256) so there are no
extra secret-bearing dependencies. Tokens are signed with SECRET_KEY which
never leaves the backend.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from app.config import Settings

PBKDF2_ITERATIONS = 260_000
_ALGORITHM = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    salt = hashlib.sha256(password.encode("utf-8")).digest()[:16]
    salt_hex = salt.hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    ).hex()
    return f"{_ALGORITHM}${PBKDF2_ITERATIONS}${salt_hex}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations_s, salt_hex, digest = stored.split("$", 3)
        if algorithm != _ALGORITHM:
            return False
        iterations = int(iterations_s)
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            iterations,
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except (ValueError, TypeError):
        return False


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_access_token(subject: str, settings: Settings) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + settings.access_token_expire_minutes * 60,
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(
        settings.secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{body}.{_b64encode(signature)}"


def decode_access_token(token: str, settings: Settings) -> str | None:
    """Return the username (sub) if the token is authentic and unexpired."""
    try:
        body, signature = token.split(".", 1)
    except ValueError:
        return None

    expected = hmac.new(
        settings.secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected, _b64decode(signature)):
        return None

    try:
        payload = json.loads(_b64decode(body))
    except (ValueError, json.JSONDecodeError):
        return None

    if int(payload.get("exp", 0)) < time.time():
        return None

    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None
