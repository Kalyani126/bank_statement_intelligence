"""API route modules."""

from app.api.routes import (
    auth,
    dashboard,
    matches,
    people,
    processing,
    rag,
    statements,
    system,
    transactions,
)

__all__ = [
    "auth",
    "dashboard",
    "matches",
    "people",
    "processing",
    "rag",
    "statements",
    "system",
    "transactions",
]
