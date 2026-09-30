"""People/counterparty endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_user
from app.api.serializers import evidence_list, serialize_match
from app.database.connection import get_db
from app.models import Person, PersonIdentifier, Statement, Transaction, User
from app.models.enums import IdentifierType
from app.models.match import TransactionPersonMatch
from app.schemas.person import (
    MatchEvidenceOut,
    PersonDetailOut,
    PersonListOut,
    PersonOut,
    PersonSummary,
)
from app.schemas.transaction import TransactionListOut, TransactionOut

router = APIRouter(prefix="/people", tags=["people"])


def _owned_person_query(db: Session, user: User):
    return (
        db.query(Person)
        .join(Transaction, Transaction.counterparty_id == Person.id)
        .join(Statement, Statement.id == Transaction.statement_id)
        .filter(Statement.user_id == user.id)
        .distinct()
    )


def _person_out(db: Session, person: Person) -> PersonOut:
    aliases = [
        identifier.value
        for identifier in person.identifiers
        if identifier.id_type == IdentifierType.NAME
    ]
    count = (
        db.query(func.count(Transaction.id))
        .filter(Transaction.counterparty_id == person.id)
        .scalar()
        or 0
    )
    return PersonOut(
        id=person.code,
        canonical_name=person.canonical_name,
        aliases=aliases,
        upi_ids=person.identifier_values(IdentifierType.UPI),
        account_identifiers=person.identifier_values(IdentifierType.ACCOUNT),
        phone_numbers=person.identifier_values(IdentifierType.PHONE),
        email_addresses=person.identifier_values(IdentifierType.EMAIL),
        source=person.source,
        first_seen_date=person.first_seen_date,
        last_seen_date=person.last_seen_date,
        transaction_count=count,
    )


def _resolve_person(db: Session, user: User, person_id: str) -> Person:
    person = db.query(Person).filter(Person.code == person_id).first()
    if person is None:
        numeric = int(person_id) if person_id.isdigit() else None
        person = (
            db.query(Person).filter(Person.id == numeric).first()
            if numeric is not None
            else None
        )
    if person is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Person not found")

    owned = (
        db.query(Transaction.id)
        .join(Statement, Statement.id == Transaction.statement_id)
        .filter(Statement.user_id == user.id, Transaction.counterparty_id == person.id)
        .first()
    )
    if owned is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Person not found")
    return person


@router.get("", response_model=PersonListOut)
def list_people(
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = _owned_person_query(db, user)
    if q:
        term = q.strip().lower()
        like = f"%{term}%"
        identifier_ids = (
            db.query(PersonIdentifier.person_id)
            .filter(
                func.lower(PersonIdentifier.normalized_value).contains(term)
                | PersonIdentifier.value.ilike(like)
            )
            .subquery()
        )
        query = query.filter(
            func.lower(Person.normalized_name).contains(term)
            | Person.canonical_name.ilike(like)
            | Person.code.ilike(like)
            | Person.id.in_(db.query(identifier_ids.c.person_id))
        )
    total = query.count()
    people = query.order_by(Person.canonical_name).offset(offset).limit(limit).all()
    return PersonListOut(
        total=total,
        items=[_person_out(db, person) for person in people],
    )


@router.get("/{person_id}", response_model=PersonDetailOut)
def get_person(
    person_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    person = _resolve_person(db, user, person_id)

    totals = (
        db.query(
            func.count(Transaction.id),
            func.coalesce(func.sum(Transaction.debit_amount), 0),
            func.coalesce(func.sum(Transaction.credit_amount), 0),
            func.min(Transaction.transaction_date),
            func.max(Transaction.transaction_date),
        )
        .filter(Transaction.counterparty_id == person.id)
        .one()
    )
    methods = [
        row[0]
        for row in db.query(Transaction.payment_method)
        .filter(
            Transaction.counterparty_id == person.id,
            Transaction.payment_method.isnot(None),
        )
        .distinct()
        .all()
    ]

    transactions = (
        db.query(Transaction)
        .filter(Transaction.counterparty_id == person.id)
        .options(joinedload(Transaction.person))
        .order_by(Transaction.transaction_date.desc().nullslast(), Transaction.id.desc())
        .limit(100)
        .all()
    )

    matches = (
        db.query(TransactionPersonMatch)
        .join(Transaction, Transaction.id == TransactionPersonMatch.transaction_id)
        .filter(TransactionPersonMatch.person_id == person.id)
        .order_by(TransactionPersonMatch.id.desc())
        .limit(20)
        .all()
    )

    detail = PersonDetailOut.model_validate(_person_out(db, person).model_dump())
    detail.summary = PersonSummary(
        total_sent=str(totals[1] or 0),
        total_received=str(totals[2] or 0),
        transaction_count=int(totals[0] or 0),
        first_transaction=str(totals[3]) if totals[3] else None,
        last_transaction=str(totals[4]) if totals[4] else None,
        payment_methods=[str(m) for m in methods if m],
    )
    detail.transactions = [TransactionOut.model_validate(t) for t in transactions]
    detail.recent_matches = [
        MatchEvidenceOut(
            transaction_id=match.transaction_id,
            status=match.status,
            method=match.method,
            confidence=match.confidence,
            reason=match.reason,
            reason_codes=list(match.reason_codes or []),
            evidence=evidence_list(match.evidence),
            transaction_date=str(match.transaction.transaction_date)
            if match.transaction and match.transaction.transaction_date
            else None,
            description=match.transaction.raw_description
            if match.transaction
            else None,
        )
        for match in matches
    ]
    return detail


@router.get("/{person_id}/transactions", response_model=TransactionListOut)
def list_person_transactions(
    person_id: str,
    method: str | None = Query(default=None, max_length=32),
    match_status: str | None = Query(default=None, max_length=16),
    txn_type: str | None = Query(default=None, alias="type", max_length=16),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.api.queries import _parse_date

    person = _resolve_person(db, user, person_id)

    query = db.query(Transaction).filter(Transaction.counterparty_id == person.id)
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

    total = query.count()
    items = (
        query.options(joinedload(Transaction.person))
        .order_by(
            Transaction.transaction_date.desc().nullslast(), Transaction.id.desc()
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    return TransactionListOut(
        total=total,
        offset=offset,
        limit=limit,
        items=[TransactionOut.model_validate(item) for item in items],
    )
