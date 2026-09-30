"""Vector store over the rag_documents table.

Structured SQL filtering always runs FIRST (PostgreSQL is the source of
truth); vectors are only scored over the pre-filtered candidate set. This
keeps exact financial lookups out of the hands of similarity search.

For very large deployments, move the embedding column to pgvector and push
the cosine ranking into SQL - the interface here stays identical.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import RagDocument, Transaction
from app.rag.documents import NewDocument

logger = logging.getLogger(__name__)

_FETCH_CAP = 2000


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def upsert_documents(db: Session, documents: list[NewDocument]) -> int:
    """Insert/refresh documents keyed by (user, doc_type, transaction)."""
    stored = 0
    for document in documents:
        query = db.query(RagDocument).filter(
            RagDocument.user_id == document.user_id,
            RagDocument.doc_type == document.doc_type,
        )
        if document.transaction_id is not None:
            query = query.filter(
                RagDocument.transaction_id == document.transaction_id
            )
        existing = query.first()
        if existing is None:
            row = RagDocument(
                user_id=document.user_id,
                statement_id=document.statement_id,
                transaction_id=document.transaction_id,
                person_id=document.person_id,
                doc_type=document.doc_type,
                content=document.content,
                meta=document.meta,
                embedding=document.embedding,
                embedding_model=document.embedding_model,
            )
            db.add(row)
        else:
            existing.content = document.content
            existing.meta = document.meta
            existing.embedding = document.embedding
            existing.embedding_model = document.embedding_model
            existing.person_id = document.person_id
        stored += 1
    return stored


@dataclass
class ScoredDocument:
    document: RagDocument
    transaction: Transaction | None
    score: float


def search(
    db: Session,
    user_id: int,
    query_vector: list[float],
    *,
    payment_method: str | None = None,
    date_from=None,
    date_to=None,
    statement_ids: list[int] | None = None,
    top_k: int = 8,
) -> list[ScoredDocument]:
    """Cosine search over this user's documents, SQL-pre-filtered."""
    query = (
        db.query(RagDocument)
        .filter(RagDocument.user_id == user_id)
        .filter(RagDocument.embedding.isnot(None))
    )
    if payment_method:
        query = query.filter(
            RagDocument.meta["payment_method"].as_string() == payment_method
        )
    if statement_ids is not None:
        # Combination scope: only the selected statements' rows are visible.
        query = query.filter(RagDocument.statement_id.in_(statement_ids or [-1]))
    documents = query.limit(_FETCH_CAP).all()

    scored: list[ScoredDocument] = []
    for document in documents:
        transaction = None
        if document.transaction_id is not None:
            transaction = db.get(Transaction, document.transaction_id)
            if transaction is None:
                continue
            if date_from and (
                transaction.transaction_date is None
                or transaction.transaction_date < date_from
            ):
                continue
            if date_to and (
                transaction.transaction_date is None
                or transaction.transaction_date > date_to
            ):
                continue
        score = cosine_similarity(query_vector, document.embedding or [])
        if score > 0:
            scored.append(
                ScoredDocument(document=document, transaction=transaction, score=score)
            )

    scored.sort(key=lambda item: -item.score)
    return scored[:top_k]
