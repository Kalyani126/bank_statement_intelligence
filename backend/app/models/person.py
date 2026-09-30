"""Person/counterparty and identifier models."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.match import TransactionPersonMatch
    from app.models.statement import Statement
    from app.models.transaction import Transaction


class Person(TimestampMixin, Base):
    __tablename__ = "persons"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stable public identifier, e.g. "P001" - used by the API and JEV payloads.
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    first_seen_date: Mapped[date | None] = mapped_column(Date)
    last_seen_date: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)

    identifiers: Mapped[list["PersonIdentifier"]] = relationship(
        back_populates="person",
        cascade="all, delete-orphan",
    )
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="person")
    matches: Mapped[list["TransactionPersonMatch"]] = relationship(
        back_populates="person",
        foreign_keys="[TransactionPersonMatch.person_id]",
    )

    def identifier_values(self, id_type: str) -> list[str]:
        return [i.value for i in self.identifiers if i.id_type == id_type]


class PersonIdentifier(TimestampMixin, Base):
    __tablename__ = "person_identifiers"
    __table_args__ = (
        UniqueConstraint(
            "person_id",
            "id_type",
            "normalized_value",
            name="uq_person_identifier",
        ),
        Index("ix_identifier_type_value", "id_type", "normalized_value"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    id_type: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="statement", nullable=False)

    person: Mapped["Person"] = relationship(back_populates="identifiers")
