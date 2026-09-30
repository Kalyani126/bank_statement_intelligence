"""Person/counterparty schemas."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from app.schemas.transaction import TransactionOut


class PersonOut(BaseModel):
    id: str  # public code, e.g. "P001"
    canonical_name: str
    aliases: list[str] = []
    upi_ids: list[str] = []
    account_identifiers: list[str] = []
    phone_numbers: list[str] = []
    email_addresses: list[str] = []
    source: str = "auto"
    first_seen_date: date | None = None
    last_seen_date: date | None = None
    transaction_count: int = 0


class PersonListOut(BaseModel):
    total: int
    items: list[PersonOut]


class PersonSummary(BaseModel):
    total_sent: str = "0.00"
    total_received: str = "0.00"
    transaction_count: int = 0
    first_transaction: str | None = None
    last_transaction: str | None = None
    payment_methods: list[str] = []


class MatchEvidenceOut(BaseModel):
    transaction_id: int
    status: str
    method: str
    confidence: float | None = None
    reason: str | None = None
    reason_codes: list[str] = []
    evidence: list[dict] = []
    transaction_date: str | None = None
    description: str | None = None


class PersonDetailOut(PersonOut):
    summary: PersonSummary = Field(default_factory=PersonSummary)
    recent_matches: list[MatchEvidenceOut] = []
    transactions: list[TransactionOut] = []
