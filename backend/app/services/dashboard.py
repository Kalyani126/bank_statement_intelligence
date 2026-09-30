"""Dashboard statistics - SQL for money totals, Python for chart buckets."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Statement, StatementStatus, Transaction, User


def _money(value) -> str:
    if value is None:
        return "0.00"
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


def summary(db: Session, user: User) -> dict:
    base = (
        db.query(
            func.count(Transaction.id),
            func.coalesce(func.sum(Transaction.debit_amount), 0),
            func.coalesce(func.sum(Transaction.credit_amount), 0),
        )
        .join(Statement, Statement.id == Transaction.statement_id)
        .filter(Statement.user_id == user.id)
    )
    total_transactions, total_debit, total_credit = base.one()

    statement_count = (
        db.query(func.count(Statement.id))
        .filter(Statement.user_id == user.id)
        .scalar()
        or 0
    )
    statements_by_status = dict(
        db.query(Statement.status, func.count(Statement.id))
        .filter(Statement.user_id == user.id)
        .group_by(Statement.status)
        .all()
    )
    unique_counterparties = (
        db.query(func.count(func.distinct(Transaction.counterparty_id)))
        .join(Statement, Statement.id == Transaction.statement_id)
        .filter(Statement.user_id == user.id)
        .filter(Transaction.counterparty_id.isnot(None))
        .scalar()
        or 0
    )
    pending_reviews = (
        db.query(func.count(Transaction.id))
        .join(Statement, Statement.id == Transaction.statement_id)
        .filter(Statement.user_id == user.id)
        .filter(Transaction.matching_status.in_(["AMBIGUOUS", "UNKNOWN"]))
        .scalar()
        or 0
    )

    # Chart buckets over light column projections (exact values from SQL rows).
    rows = (
        db.query(
            Transaction.transaction_date,
            Transaction.debit_amount,
            Transaction.credit_amount,
            Transaction.payment_method,
        )
        .join(Statement, Statement.id == Transaction.statement_id)
        .filter(Statement.user_id == user.id)
        .limit(20000)
        .all()
    )

    monthly: dict[str, dict] = defaultdict(
        lambda: {"debit": Decimal("0"), "credit": Decimal("0"), "count": 0}
    )
    methods: dict[str, dict] = defaultdict(
        lambda: {"debit": Decimal("0"), "credit": Decimal("0"), "count": 0}
    )
    activity: dict[str, int] = defaultdict(int)
    today = date.today()

    for transaction_date, debit, credit, method in rows:
        period = (
            transaction_date.strftime("%Y-%m") if transaction_date else "unknown"
        )
        bucket = monthly[period]
        bucket["debit"] += Decimal(str(debit or 0))
        bucket["credit"] += Decimal(str(credit or 0))
        bucket["count"] += 1

        method_bucket = methods[method or "OTHER"]
        method_bucket["debit"] += Decimal(str(debit or 0))
        method_bucket["credit"] += Decimal(str(credit or 0))
        method_bucket["count"] += 1

        if transaction_date:
            activity[transaction_date.isoformat()] += 1

    monthly_series = [
        {"period": period, "debit": _money(data["debit"]), "credit": _money(data["credit"]), "count": data["count"]}
        for period, data in sorted(monthly.items())
    ]
    method_series = [
        {"method": method, "count": data["count"], "debit": _money(data["debit"]), "credit": _money(data["credit"])}
        for method, data in sorted(methods.items(), key=lambda item: -item[1]["count"])
    ]

    # Last 30 days of activity for the timeline chart.
    activity_series = []
    for offset in range(29, -1, -1):
        day = today - timedelta(days=offset)
        key = day.isoformat()
        activity_series.append({"date": key, "count": activity.get(key, 0)})

    return {
        "totals": {
            "statements": statement_count,
            "transactions": total_transactions,
            "total_debit": _money(total_debit),
            "total_credit": _money(total_credit),
            "unique_counterparties": unique_counterparties,
            "pending_reviews": pending_reviews,
        },
        "statements_by_status": {
            str(status): int(count) for status, count in statements_by_status.items()
        },
        "charts": {
            "monthly": monthly_series,
            "payment_methods": method_series,
            "activity": activity_series,
        },
    }
