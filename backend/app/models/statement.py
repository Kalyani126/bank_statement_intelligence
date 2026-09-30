"""Statement and uploaded file models."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import StatementStatus

if TYPE_CHECKING:
    from app.models.match import TransactionPersonMatch
    from app.models.statement import Statement
    from app.models.transaction import Transaction
    from app.models.user import User
    from app.models.job import ProcessingJob


class Statement(TimestampMixin, Base):
    __tablename__ = "statements"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    bank_name: Mapped[str | None] = mapped_column(String(128))
    account_holder_name: Mapped[str | None] = mapped_column(String(255))
    account_number_masked: Mapped[str | None] = mapped_column(String(32))
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    source_file_name: Mapped[str | None] = mapped_column(String(255))
    file_type: Mapped[str | None] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(
        String(16),
        default=StatementStatus.UPLOADED,
        index=True,
        nullable=False,
    )
    transaction_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="statements")

    @property
    def uploaded_at(self) -> datetime | None:
        """Alias for created_at (API field name)."""
        return self.created_at
    files: Mapped[list["StatementFile"]] = relationship(
        back_populates="statement",
        cascade="all, delete-orphan",
    )
    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="statement",
        cascade="all, delete-orphan",
    )
    jobs: Mapped[list["ProcessingJob"]] = relationship(
        back_populates="statement",
        cascade="all, delete-orphan",
    )


class StatementFile(TimestampMixin, Base):
    """Metadata of the uploaded file.

    PDF passwords are NEVER stored here (or anywhere) - they are only held in
    memory for the duration of a single extraction attempt.
    """

    __tablename__ = "statement_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(
        ForeignKey("statements.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    is_encrypted: Mapped[bool] = mapped_column(default=False, nullable=False)

    statement: Mapped["Statement"] = relationship(back_populates="files")


# Re-export for convenience.
__all__ = ["Statement", "StatementFile"]
