"""Pydantic schemas for the JEV decision layer."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

MatchStatus = Literal["MATCH", "AMBIGUOUS", "UNKNOWN"]


class JEVTransaction(BaseModel):
    """Structured transaction facts handed to JEV (no raw DB dump)."""

    raw_description: str
    normalized_name: str | None = None
    name_tokens: list[str] = Field(default_factory=list)
    normalized_upi: str | None = None
    account_identifier: str | None = None
    reference: str | None = None
    payment_method: str | None = None
    transaction_date: str | None = None
    direction: str | None = None
    amount: str | None = None


class JEVCandidate(BaseModel):
    person_id: str  # public code, e.g. "P001"
    name: str
    aliases: list[str] = Field(default_factory=list)
    upi_ids: list[str] = Field(default_factory=list)
    account_identifiers: list[str] = Field(default_factory=list)
    phone_numbers: list[str] = Field(default_factory=list)
    email_addresses: list[str] = Field(default_factory=list)
    local_evidence: list[str] = Field(default_factory=list)
    local_score: float | None = None


class JEVDecisionRequest(BaseModel):
    transaction: JEVTransaction
    candidates: list[JEVCandidate]


class JEVDecision(BaseModel):
    status: MatchStatus
    person_id: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    # Real confidence reported by TypeSafe/Jev (never fabricated).
    confidence: float | None = None
    model: str | None = None
    raw: dict[str, Any] | None = None
