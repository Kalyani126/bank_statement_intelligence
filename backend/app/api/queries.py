"""Shared query helpers for transaction filtering and search."""

from __future__ import annotations

from datetime import date as date_type

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Person, PersonIdentifier, Statement, Transaction, User


def _parse_date(value: str | None) -> date_type | None:
    if not value:
        return None
    from app.utils.dates import parse_date_any

    return parse_date_any(value)


def _try_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def apply_transaction_filters(
    query,
    q: str | None,
    person: str | None,
    method: str | None,
    match_status: str | None,
    txn_type: str | None,
    date_from: str | None,
    date_to: str | None,
    db: Session,
    user: User,
):
    """Apply the standard filter set; `q` searches names, UPI, accounts, refs."""
    query = query.join(Statement, Statement.id == Transaction.statement_id).filter(
        Statement.user_id == user.id
    )

    if q:
        term = q.strip().lower()
        like = f"%{term}%"

        person_ids = select(Person.id).where(
            or_(
                Person.normalized_name.contains(term),
                Person.code.ilike(like),
                Person.canonical_name.ilike(like),
            )
        )
        identifier_sub = (
            select(PersonIdentifier.person_id)
            .where(
                or_(
                    PersonIdentifier.normalized_value.contains(term),
                    PersonIdentifier.value.ilike(like),
                )
            )
        )
        query = query.filter(
            or_(
                Transaction.counterparty_id.in_(person_ids),
                Transaction.counterparty_id.in_(identifier_sub),
                func.lower(Transaction.counterparty_name).contains(term),
                Transaction.normalized_counterparty_name.contains(term),
                func.lower(Transaction.upi_id).contains(term),
                Transaction.normalized_upi_id.contains(term),
                func.lower(Transaction.reference_number).contains(term),
                func.lower(Transaction.description).contains(term),
                func.lower(Transaction.raw_text).contains(term),
            )
        )

    if person:
        numeric_id = _try_int(person)
        person_conditions = [Person.code == person]
        if numeric_id is not None:
            person_conditions.append(Person.id == numeric_id)
        query = query.join(
            Person, Person.id == Transaction.counterparty_id, isouter=True
        ).filter(or_(*person_conditions))
    if method:
        query = query.filter(Transaction.payment_method == method.upper())
    if match_status:
        query = query.filter(Transaction.matching_status == match_status.upper())
    if txn_type:
        query = query.filter(Transaction.transaction_type == txn_type.upper())

    parsed_from = _parse_date(date_from)
    if parsed_from:
        query = query.filter(Transaction.transaction_date >= parsed_from)
    parsed_to = _parse_date(date_to)
    if parsed_to:
        query = query.filter(Transaction.transaction_date <= parsed_to)

    return query
