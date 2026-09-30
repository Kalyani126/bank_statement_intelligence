"""RAG query service - the AI/assistant endpoint's backend.

Responsibility split (enforced by this pipeline):
  JEV/matching layer -> who does this transaction belong to?
  RAG                -> which transactions are relevant to the question?
  PostgreSQL         -> what records actually exist?
  SQL/Python         -> what is the exact financial calculation?
  LLM (optional)     -> how do we explain the verified result?
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditAction, User
from app.rag.answer import compose_answer
from app.rag.context_builder import build_context
from app.rag.retrieval import (
    compute_aggregates,
    compute_closing_balance,
    compute_counterparties,
    resolve_statement_scope,
    retrieve,
    understand_question,
)
from app.security.audit import write_audit

logger = logging.getLogger(__name__)


def query(
    db: Session,
    settings: Settings,
    user: User,
    question: str,
    ip_address: str | None = None,
    statement_ids: list[int] | None = None,
    banks: list[str] | None = None,
) -> dict[str, Any]:
    scope = resolve_statement_scope(db, user.id, statement_ids, banks)
    profile = understand_question(question)
    retrieval = retrieve(db, settings, user.id, profile, statement_scope=scope)

    ambiguous = any(
        note in retrieval.notes
        for note in ("MULTIPLE_PERSON_MATCHES", "MULTIPLE_POSSIBLE_MATCHES")
    )

    aggregates: dict[str, Any] | None = None
    person_ok = (
        not profile.person_text
        or retrieval.person_resolution.status == "resolved"
        or "MATCHED_BY_DESCRIPTION_TEXT" in retrieval.notes
    )
    if retrieval.structured_complete and not ambiguous and person_ok:
        aggregates = compute_aggregates(db, user.id, retrieval.filters)
        if profile.intent in ("top_counterparty", "counterparties"):
            aggregates["counterparties"] = compute_counterparties(
                db,
                user.id,
                retrieval.filters,
                limit=10,
                # "who did I send the most money to" ranks by money,
                # "who do I transact with" ranks by how often.
                rank_by=("amount" if profile.intent == "top_counterparty" else "count"),
            )
        elif profile.intent == "balance":
            aggregates.update(
                compute_closing_balance(db, user.id, retrieval.filters)
            )

    context = build_context(profile, retrieval, aggregates)
    answer = compose_answer(settings, context)

    write_audit(
        db,
        AuditAction.RAG_QUERY,
        user_id=user.id,
        details={
            "question": question,
            "intent": profile.intent,
            "answer_source": answer.answer_source,
            "explanation_source": answer.explanation_source,
            "scope_statement_ids": scope,
            "scope_banks": banks,
        },
        ip_address=ip_address,
    )
    db.commit()

    resolution = retrieval.person_resolution
    notes = list(answer.notes)
    if scope:
        # Make the combination scope visible in the UI (SCOPE_EMPTY comes
        # back from retrieval itself when nothing matched the selection).
        notes.append("SCOPE_SELECTED")
    return {
        "answer": answer.text,
        "answer_source": answer.answer_source,
        "explanation_source": answer.explanation_source,
        "source_label": (
            "Answer based on the selected statements."
            if scope is not None
            else "Answer based on statement transactions."
        ),
        "intent": profile.intent,
        "aggregates": answer.aggregates,
        "person_resolution": resolution.status,
        "person_candidates": answer.person_candidates,
        "supporting_transactions": answer.supporting,
        "notes": notes,
        "scope": {
            "statement_ids": scope,
            "banks": banks,
        },
    }
