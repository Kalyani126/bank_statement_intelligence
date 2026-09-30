"""Manual review schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.transaction import TransactionOut


class CandidateEvidence(BaseModel):
    code: str
    weight: float | None = None
    strength: str | None = None
    detail: str | None = None


class CandidateOut(BaseModel):
    person_id: str
    name: str
    score: float | None = None
    evidence: list[CandidateEvidence] = []


class MatchOut(BaseModel):
    id: int
    transaction_id: int
    person_id: str | None = None
    suggested_person_id: str | None = None
    status: str
    method: str
    confidence: float | None = None
    reason_codes: list[str] = []
    reason: str | None = None
    candidates: list[CandidateOut] = []
    evidence: list[CandidateEvidence] = []
    needs_review: bool = False
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None


class AmbiguousItem(BaseModel):
    transaction: TransactionOut
    match: MatchOut | None = None


class AmbiguousListOut(BaseModel):
    total: int
    items: list[AmbiguousItem]


class ConfirmRequest(BaseModel):
    person_code: str | None = Field(
        default=None, description="Person code (e.g. P001); defaults to the suggestion"
    )
    note: str | None = Field(default=None, max_length=500)


class RejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)
