"""AI/RAG assistant schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class RAGQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    # Optional scope: answer only from these statements / banks.
    # Unset (None) = every statement of the user, as before.
    statement_ids: list[int] | None = Field(
        default=None,
        description="Combine only these statements (max 20).",
    )
    banks: list[str] | None = Field(
        default=None,
        max_length=20,
        description="Combine only statements of these banks (case-insensitive).",
    )


class SupportingTransaction(BaseModel):
    transaction_id: int
    date: str | None = None
    description: str | None = None
    counterparty: str | None = None
    person_code: str | None = None
    payment_method: str | None = None
    type: str | None = None
    amount: str | None = None
    matching_status: str | None = None


class PersonCandidate(BaseModel):
    code: str
    name: str


class RAGScope(BaseModel):
    """Which statements the answer was computed from (null = all of them)."""

    statement_ids: list[int] | None = None
    banks: list[str] | None = None


class RAGQueryResponse(BaseModel):
    answer: str
    answer_source: Literal[
        "STRUCTURED_SEARCH", "RAG", "RAG_PLUS_STRUCTURED_SEARCH"
    ]
    explanation_source: Literal["TEMPLATE", "LLM"]
    source_label: str
    intent: str
    aggregates: dict[str, Any] = Field(default_factory=dict)
    person_resolution: str = "none"
    person_candidates: list[PersonCandidate] = Field(default_factory=list)
    supporting_transactions: list[SupportingTransaction] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    scope: RAGScope = Field(default_factory=RAGScope)
