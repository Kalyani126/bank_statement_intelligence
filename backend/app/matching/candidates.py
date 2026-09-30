"""PostgreSQL candidate search.

JEV is NOT the database search engine: candidates are always found with
structured SQL first, then handed to the decision layer (JEV or local).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, literal, or_
from sqlalchemy.orm import Session

from app.models.enums import IdentifierType, PaymentMethod
from app.models.person import Person, PersonIdentifier
from app.models.transaction import Transaction
from app.services.normalization import NormalizedTransaction

_MAX_ROWS = 200
_DEFAULT_LIMIT = 8


@dataclass
class CandidateHit:
    person: Person
    hints: list[str] = field(default_factory=list)


def search_candidates(
    db: Session,
    norm: NormalizedTransaction,
    limit: int = _DEFAULT_LIMIT,
) -> list[CandidateHit]:
    """Find every person that could plausibly match this transaction."""
    found: dict[int, CandidateHit] = {}

    def add(person: Person | None, hint: str) -> None:
        if person is None:
            return
        entry = found.get(person.id)
        if entry is None:
            found[person.id] = CandidateHit(person=person, hints=[hint])
        elif hint not in entry.hints:
            entry.hints.append(hint)

    # --- UPI identifiers (exact, partial, same-domain) ------------------------
    if norm.normalized_upi_id:
        handle = norm.normalized_upi_id.split("@", 1)[0]
        domain = norm.normalized_upi_id.split("@", 1)[1]
        rows = (
            db.query(PersonIdentifier)
            .filter(PersonIdentifier.id_type == IdentifierType.UPI)
            .filter(
                or_(
                    PersonIdentifier.normalized_value == norm.normalized_upi_id,
                    PersonIdentifier.normalized_value.like(f"%{handle}%"),
                    PersonIdentifier.normalized_value.like(f"%@{domain}"),
                )
            )
            .limit(_MAX_ROWS)
            .all()
        )
        for identifier in rows:
            value = identifier.normalized_value
            value_handle = value.split("@", 1)[0]
            if value == norm.normalized_upi_id:
                hint = "upi_exact"
            elif handle.startswith(value_handle) or value_handle.startswith(handle):
                hint = "upi_partial"
            elif value.endswith(f"@{domain}"):
                hint = "upi_domain"
            else:
                continue
            add(db.get(Person, identifier.person_id), hint)

    # --- Account identifiers ---------------------------------------------------
    if norm.account_identifier:
        last4 = norm.account_identifier[-4:]
        rows = (
            db.query(PersonIdentifier)
            .filter(PersonIdentifier.id_type == IdentifierType.ACCOUNT)
            .filter(
                or_(
                    PersonIdentifier.normalized_value == norm.account_identifier,
                    PersonIdentifier.normalized_value.like(f"%{last4}"),
                )
            )
            .limit(_MAX_ROWS)
            .all()
        )
        for identifier in rows:
            hint = (
                "account_exact"
                if identifier.normalized_value == norm.account_identifier
                else "account_last4"
            )
            add(db.get(Person, identifier.person_id), hint)

    # --- Phone / email ---------------------------------------------------------
    for value, hint in (
        (norm.phone, "phone"),
        (norm.email, "email"),
    ):
        if not value:
            continue
        rows = (
            db.query(PersonIdentifier)
            .filter(PersonIdentifier.normalized_value == value)
            .limit(50)
            .all()
        )
        for identifier in rows:
            add(db.get(Person, identifier.person_id), hint)

    # --- Name tokens (both directions, boundary-checked) ------------------------
    conditions = []
    for token in {t.lower() for t in norm.name_tokens}:
        if len(token) >= 3:
            conditions.append(Person.normalized_name.contains(token))
    if norm.normalized_name and len(norm.normalized_name) >= 3:
        conditions.append(
            literal(norm.normalized_name).contains(Person.normalized_name)
        )

    if conditions:
        people = (
            db.query(Person)
            .filter(or_(*conditions))
            .order_by(Person.id)
            .limit(_MAX_ROWS)
            .all()
        )
        txn_tokens = {t.lower() for t in norm.name_tokens}
        for person in people:
            person_tokens = set((person.normalized_name or "").split())
            if not txn_tokens or not person_tokens:
                continue
            # Whole-token overlap only: "ravi" must not match "ravindra".
            if txn_tokens & person_tokens or person_tokens <= {
                t.lower() for t in norm.name_tokens
            }:
                add(person, "name")

    hits = list(found.values())
    hits.sort(key=lambda hit: hit.person.id)
    return hits[:limit]


def load_person_methods(db: Session, person_ids: list[int]) -> dict[int, set[str]]:
    """Payment methods already seen on each person's matched transactions."""
    if not person_ids:
        return {}
    rows = (
        db.query(Transaction.counterparty_id, Transaction.payment_method)
        .filter(Transaction.counterparty_id.in_(person_ids))
        .filter(Transaction.payment_method.isnot(None))
        .filter(Transaction.payment_method != PaymentMethod.OTHER)
        .distinct()
        .all()
    )
    methods: dict[int, set[str]] = {}
    for person_id, method in rows:
        methods.setdefault(person_id, set()).add(method)
    return methods


def count_people(db: Session) -> int:
    return db.query(func.count(Person.id)).scalar() or 0
