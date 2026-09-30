"""Transaction schemas."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel


class TransactionOut(BaseModel):
    id: int
    statement_id: int
    transaction_date: date | None = None
    value_date: date | None = None
    description: str | None = None
    raw_description: str | None = None
    narration: str | None = None
    reference_number: str | None = None
    debit_amount: Decimal | None = None
    credit_amount: Decimal | None = None
    balance: Decimal | None = None
    currency: str = "INR"
    transaction_type: str = "UNKNOWN"
    payment_method: str | None = None
    upi_id: str | None = None
    normalized_upi_id: str | None = None
    counterparty_name: str | None = None
    normalized_counterparty_name: str | None = None
    counterparty_code: str | None = None
    matching_status: str | None = None
    matching_method: str | None = None
    matching_confidence: float | None = None
    matching_reason: str | None = None
    source_page: int | None = None
    raw_text: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TransactionListOut(BaseModel):
    total: int
    offset: int = 0
    limit: int = 50
    items: list[TransactionOut]
