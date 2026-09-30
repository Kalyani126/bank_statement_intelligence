"""RAG document builders (transaction -> indexable text)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.models.enums import DocumentType
from app.models.transaction import Transaction


@dataclass
class NewDocument:
    user_id: int
    statement_id: int | None
    transaction_id: int | None
    person_id: int | None
    doc_type: str
    content: str
    meta: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None
    embedding_model: str | None = None

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def build_transaction_content(transaction: Transaction) -> str:
    """Compact, search-friendly rendering of one transaction.

    The raw description is included verbatim so retrieval can find counterparty
    spellings that normalization did not keep.
    """
    date_part = str(transaction.transaction_date) if transaction.transaction_date else "unknown date"
    counterparty = transaction.counterparty_name or "Unknown counterparty"
    direction = "DEBIT" if transaction.transaction_type == "DEBIT" else (
        "CREDIT" if transaction.transaction_type == "CREDIT" else "UNKNOWN"
    )
    if transaction.debit_amount is not None:
        amount = f"Rs {transaction.debit_amount}"
    elif transaction.credit_amount is not None:
        amount = f"Rs {transaction.credit_amount}"
    else:
        amount = "amount unknown"
    method = transaction.payment_method or "OTHER"
    raw = transaction.raw_description or transaction.description or ""
    return (
        f"{date_part} | {counterparty} | {direction} | {amount} | "
        f"{method} | {raw}"
    )


def build_transaction_document(transaction: Transaction) -> NewDocument:
    return NewDocument(
        user_id=transaction.statement.user_id if transaction.statement else 0,
        statement_id=transaction.statement_id,
        transaction_id=transaction.id,
        person_id=transaction.counterparty_id,
        doc_type=DocumentType.TRANSACTION,
        content=build_transaction_content(transaction),
        meta={
            "date": str(transaction.transaction_date)
            if transaction.transaction_date
            else None,
            "direction": transaction.transaction_type,
            "payment_method": transaction.payment_method,
            "counterparty": transaction.counterparty_name,
            "person_id": transaction.counterparty_id,
        },
    )
