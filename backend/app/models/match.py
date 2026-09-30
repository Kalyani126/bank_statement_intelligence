"""Transaction <-> person match records.

One row per transaction holding the current decision (automatic or manual)
including the evidence, candidate list and reason codes.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Float, ForeignKey, JSON, String, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.person import Person
    from app.models.transaction import Transaction


class TransactionPersonMatch(TimestampMixin, Base):
    __tablename__ = "transaction_person_matches"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL"),
        index=True,
    )
    suggested_person_id: Mapped[int | None] = mapped_column(
        ForeignKey("persons.id", ondelete="SET NULL")
    )

    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    method: Mapped[str] = mapped_column(String(24), nullable=False)
    # Only populated when the implementing layer actually calculates it
    # (deterministic evidence score or JEV-provided confidence). Never faked.
    confidence: Mapped[float | None] = mapped_column(Float)
    reason_codes: Mapped[list[Any] | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(Text)
    # Candidate people considered for this transaction.
    candidates: Mapped[list[Any] | None] = mapped_column(JSON)
    # Structured evidence used for the decision.
    evidence: Mapped[list[Any] | None] = mapped_column(JSON)

    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    transaction: Mapped["Transaction"] = relationship(back_populates="match")
    person: Mapped["Person | None"] = relationship(
        foreign_keys=[person_id],
        back_populates="matches",
    )
    suggested_person: Mapped["Person | None"] = relationship(
        foreign_keys=[suggested_person_id],
    )
