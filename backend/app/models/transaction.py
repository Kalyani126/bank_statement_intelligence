"""Transaction model.

Raw source data (raw_text / raw_description) is preserved verbatim and is
never overwritten; every derived field lives in its own column.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.match import TransactionPersonMatch
    from app.models.person import Person
    from app.models.statement import Statement


class Transaction(TimestampMixin, Base):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_upi", "normalized_upi_id"),
        Index("ix_transactions_counterparty_name", "normalized_counterparty_name"),
        Index("ix_transactions_date", "transaction_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("statements.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # --- Dates ---------------------------------------------------------------
    transaction_date: Mapped[date | None] = mapped_column(Date)
    value_date: Mapped[date | None] = mapped_column(Date)

    # --- Raw source data (never modified after insert) -----------------------
    description: Mapped[str | None] = mapped_column(Text)
    raw_description: Mapped[str | None] = mapped_column(Text)
    narration: Mapped[str | None] = mapped_column(Text)
    raw_text: Mapped[str | None] = mapped_column(Text)
    source_page: Mapped[int | None] = mapped_column()

    # --- Financials ----------------------------------------------------------
    reference_number: Mapped[str | None] = mapped_column(String(128), index=True)
    debit_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    credit_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(8), default="INR", nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(16), default="UNKNOWN", index=True)

    # --- Normalized counterparty data ---------------------------------------
    payment_method: Mapped[str | None] = mapped_column(String(32), index=True)
    upi_id: Mapped[str | None] = mapped_column(String(255))
    normalized_upi_id: Mapped[str | None] = mapped_column(String(255))
    counterparty_name: Mapped[str | None] = mapped_column(String(255))
    normalized_counterparty_name: Mapped[str | None] = mapped_column(String(255))
    counterparty_id: Mapped[int | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL"),
        index=True,
    )

    # --- Matching outcome (source of truth is transaction_person_matches) ----
    matching_status: Mapped[str | None] = mapped_column(String(16), index=True)
    matching_method: Mapped[str | None] = mapped_column(String(24))
    matching_confidence: Mapped[float | None] = mapped_column(Float)
    matching_reason: Mapped[str | None] = mapped_column(Text)

    statement: Mapped["Statement"] = relationship(back_populates="transactions")
    person: Mapped["Person | None"] = relationship(back_populates="transactions")
    match: Mapped["TransactionPersonMatch | None"] = relationship(
        back_populates="transaction",
        cascade="all, delete-orphan",
        uselist=False,
    )

    @property
    def amount(self) -> Decimal | None:
        return self.debit_amount if self.debit_amount is not None else self.credit_amount

    @property
    def counterparty_code(self) -> str | None:
        return self.person.code if self.person is not None else None
