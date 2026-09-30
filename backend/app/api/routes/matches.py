"""Manual review endpoints: ambiguous list, confirm, reject.

Every manual decision is persisted with reviewer, timestamp and an audit log.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session, joinedload

from app.api.deps import client_ip, get_current_user
from app.api.serializers import serialize_match
from app.database.connection import get_db
from app.matching.service import attach_alias
from app.models import (
    AuditAction,
    Person,
    Statement,
    Transaction,
    TransactionPersonMatch,
    User,
)
from app.models.enums import MatchingMethod, MatchingStatus
from app.schemas.match import (
    AmbiguousItem,
    AmbiguousListOut,
    ConfirmRequest,
    MatchOut,
    RejectRequest,
)
from app.schemas.transaction import TransactionOut
from app.security.audit import write_audit
from app.services.normalization import normalize_description

router = APIRouter(prefix="/matches", tags=["matches"])


class ReviewResponse(TransactionOut):
    match: MatchOut | None = None


def _owned_transaction(db: Session, user: User, transaction_id: int) -> Transaction:
    transaction = (
        db.query(Transaction)
        .join(Statement, Statement.id == Transaction.statement_id)
        .filter(Transaction.id == transaction_id, Statement.user_id == user.id)
        .first()
    )
    if transaction is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transaction not found")
    return transaction


@router.get("/ambiguous", response_model=AmbiguousListOut)
def list_ambiguous(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = (
        db.query(Transaction)
        .join(Statement, Statement.id == Transaction.statement_id)
        .filter(Statement.user_id == user.id)
        .join(
            TransactionPersonMatch,
            TransactionPersonMatch.transaction_id == Transaction.id,
        )
        .filter(TransactionPersonMatch.needs_review.is_(True))
    )
    total = query.count()
    items = (
        query.options(
            joinedload(Transaction.match).joinedload(TransactionPersonMatch.person),
            joinedload(Transaction.match).joinedload(
                TransactionPersonMatch.suggested_person
            ),
            joinedload(Transaction.person),
        )
        .order_by(Transaction.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return AmbiguousListOut(
        total=total,
        items=[
            AmbiguousItem(
                transaction=TransactionOut.model_validate(transaction),
                match=serialize_match(transaction.match),
            )
            for transaction in items
        ],
    )


def _finish(db: Session, transaction: Transaction) -> ReviewResponse:
    db.refresh(transaction)
    return ReviewResponse(
        **TransactionOut.model_validate(transaction).model_dump(),
        match=serialize_match(transaction.match),
    )


@router.post("/{transaction_id}/confirm", response_model=ReviewResponse)
def confirm_match(
    transaction_id: int,
    payload: ConfirmRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    transaction = _owned_transaction(db, user, transaction_id)
    match = transaction.match

    person_code = payload.person_code
    if not person_code and match is not None and match.suggested_person_id is not None:
        suggested = db.get(Person, match.suggested_person_id)
        person_code = suggested.code if suggested else None
    if not person_code:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No candidate selected. Provide person_code.",
        )

    person = db.query(Person).filter(Person.code == person_code).first()
    if person is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Candidate person not found")

    norm = normalize_description(
        transaction.raw_description or transaction.description or ""
    )
    now = datetime.now(timezone.utc)

    transaction.counterparty_id = person.id
    transaction.matching_status = MatchingStatus.MATCH
    transaction.matching_method = MatchingMethod.MANUAL
    transaction.matching_confidence = None  # manual decisions carry no score
    transaction.matching_reason = f"Manually confirmed by {user.username}"

    if match is None:
        match = TransactionPersonMatch(transaction=transaction)
        db.add(match)
    match.person_id = person.id
    match.status = MatchingStatus.MATCH
    match.method = MatchingMethod.MANUAL
    match.confidence = None
    match.reason_codes = ["MANUAL_CONFIRM"]
    match.reason = payload.note or f"Manually confirmed by {user.username}"
    match.needs_review = False
    match.reviewed_by = user.username
    match.reviewed_at = now

    attach_alias(db, person, norm)
    if transaction.transaction_date:
        if person.first_seen_date is None or transaction.transaction_date < person.first_seen_date:
            person.first_seen_date = transaction.transaction_date
        if person.last_seen_date is None or transaction.transaction_date > person.last_seen_date:
            person.last_seen_date = transaction.transaction_date

    write_audit(
        db,
        AuditAction.MANUAL_MATCH_CONFIRM,
        user_id=user.id,
        entity_type="transaction",
        entity_id=transaction.id,
        details={"person_code": person.code, "note": payload.note},
        ip_address=client_ip(request),
    )
    db.commit()
    return _finish(db, transaction)


@router.post("/{transaction_id}/reject", response_model=ReviewResponse)
def reject_match(
    transaction_id: int,
    payload: RejectRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    transaction = _owned_transaction(db, user, transaction_id)
    match = transaction.match
    now = datetime.now(timezone.utc)

    transaction.counterparty_id = None
    transaction.matching_status = MatchingStatus.UNKNOWN
    transaction.matching_method = MatchingMethod.MANUAL
    transaction.matching_confidence = None
    transaction.matching_reason = payload.reason or "Marked unknown by reviewer"

    if match is None:
        match = TransactionPersonMatch(transaction=transaction)
        db.add(match)
    match.person_id = None
    match.status = MatchingStatus.UNKNOWN
    match.method = MatchingMethod.MANUAL
    match.confidence = None
    match.reason_codes = ["MANUAL_UNKNOWN"]
    match.reason = payload.reason or "Marked unknown by reviewer"
    match.needs_review = False
    match.reviewed_by = user.username
    match.reviewed_at = now

    write_audit(
        db,
        AuditAction.MANUAL_MATCH_REJECT,
        user_id=user.id,
        entity_type="transaction",
        entity_id=transaction.id,
        details={"reason": payload.reason},
        ip_address=client_ip(request),
    )
    db.commit()
    return _finish(db, transaction)
