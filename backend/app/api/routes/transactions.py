"""Global transaction listing and search."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_user
from app.api.queries import apply_transaction_filters
from app.database.connection import get_db
from app.models import Statement, Transaction, User
from app.schemas.transaction import TransactionListOut, TransactionOut

router = APIRouter(prefix="/transactions", tags=["transactions"])


def _parse_statement_ids(raw: str | None) -> list[int]:
    """'1,2,3' -> [1, 2, 3]; junk entries ignored, capped at 20 ids."""
    if not raw:
        return []
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value > 0 and value not in ids:
            ids.append(value)
        if len(ids) >= 20:
            break
    return ids


@router.get("", response_model=TransactionListOut)
def list_transactions(
    q: str | None = Query(
        default=None,
        max_length=200,
        description="Search name, UPI id, masked account, reference, description",
    ),
    person: str | None = Query(default=None, max_length=64),
    method: str | None = Query(default=None, max_length=32),
    match_status: str | None = Query(default=None, max_length=16),
    txn_type: str | None = Query(default=None, alias="type", max_length=16),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    statement_id: int | None = Query(default=None),
    statement_ids: str | None = Query(
        default=None,
        description="Comma-separated statement ids to combine, e.g. '1,2'.",
    ),
    bank: str | None = Query(
        default=None, max_length=64,
        description="Exact bank name (case-insensitive), e.g. 'HDFC Bank'.",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Transaction)
    if statement_id:
        query = query.filter(Transaction.statement_id == statement_id)
    combined_ids = _parse_statement_ids(statement_ids)
    if combined_ids:
        query = query.filter(Transaction.statement_id.in_(combined_ids))
    query = apply_transaction_filters(
        query, q, person, method, match_status, txn_type, date_from, date_to, db, user
    )
    if bank:
        # The user scoping join above already happened in apply_transaction_filters.
        query = query.filter(func.lower(Statement.bank_name) == bank.strip().lower())
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
