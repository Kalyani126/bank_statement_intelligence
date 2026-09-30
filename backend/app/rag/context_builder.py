"""Builds the verified context handed to answer composition.

Everything numeric in here is computed by SQL; the answer layer may only
phrase these values, never recompute them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.models import Person, Transaction
from app.rag.retrieval import PersonResolution, QuestionProfile, RetrievalResult


@dataclass
class QueryContext:
    question: str
    profile: QuestionProfile
    retrieval: RetrievalResult
    aggregates: dict[str, Any] | None
    person_label: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def mode(self) -> str:
        return self.retrieval.mode

    @property
    def rows(self) -> list[Transaction]:
        return self.retrieval.rows


def person_label(resolution: PersonResolution, profile: QuestionProfile) -> str | None:
    if resolution.status == "resolved" and resolution.persons:
        person: Person = resolution.persons[0]
        return f"{person.canonical_name} ({person.code})"
    if resolution.status == "ambiguous" and resolution.persons:
        return ", ".join(
            f"{person.canonical_name} ({person.code})"
            for person in resolution.persons
        )
    return profile.person_text


def supporting_transactions(rows: list[Transaction], limit: int = 25) -> list[dict]:
    """Serialise verified rows for the UI (traceable back to the statement)."""
    output: list[dict] = []
    for transaction in rows[:limit]:
        amount = (
            transaction.debit_amount
            if transaction.debit_amount is not None
            else transaction.credit_amount
        )
        output.append(
            {
                "transaction_id": transaction.id,
                "date": str(transaction.transaction_date)
                if transaction.transaction_date
                else None,
                "description": transaction.raw_description
                or transaction.description,
                "counterparty": transaction.counterparty_name,
                "person_code": transaction.person.code if transaction.person else None,
                "payment_method": transaction.payment_method,
                "type": transaction.transaction_type,
                "amount": str(amount) if amount is not None else None,
                "matching_status": transaction.matching_status,
            }
        )
    return output


def format_inr(amount: Decimal | float | int | None) -> str:
    if amount is None:
        return "0.00"
    value = Decimal(str(amount)).quantize(Decimal("0.01"))
    return f"{value:,.2f}"


def build_context(
    profile: QuestionProfile,
    retrieval: RetrievalResult,
    aggregates: dict[str, Any] | None,
) -> QueryContext:
    return QueryContext(
        question=profile.raw_question,
        profile=profile,
        retrieval=retrieval,
        aggregates=aggregates,
        person_label=person_label(retrieval.person_resolution, profile),
        notes=list(retrieval.notes),
    )
