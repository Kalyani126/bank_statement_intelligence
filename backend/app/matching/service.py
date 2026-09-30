"""Matching pipeline orchestration.

Flow: normalize -> PostgreSQL candidate search -> (strong-identifier person
creation when needed) -> JEV decision when enabled else LOCAL_MATCHING ->
persist outcome on the transaction + match record.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.jev.service import resolve as jev_resolve
from app.matching.candidates import load_person_methods, search_candidates
from app.matching.evidence import EvidenceItem, MatchContext
from app.matching.local_matcher import (
    CandidateResult,
    MatchOutcome,
    decide,
    evaluate_candidates,
)
from app.models import MatchingMethod, MatchingStatus, TransactionPersonMatch
from app.models.enums import IdentifierType, PersonSource
from app.models.person import Person, PersonIdentifier
from app.models.transaction import Transaction
from app.services.normalization import NormalizedTransaction, normalize_name

logger = logging.getLogger(__name__)

_STRONG_CODES = {
    "UPI_EXACT_MATCH",
    "ACCOUNT_EXACT_MATCH",
    "PHONE_EXACT_MATCH",
    "EMAIL_EXACT_MATCH",
    "REFERENCE_MATCH",
}


def _derive_canonical_name(norm: NormalizedTransaction) -> str:
    if norm.name:
        return norm.name
    if norm.normalized_upi_id:
        local = norm.normalized_upi_id.split("@", 1)[0]
        words = [part.title() for part in local.replace(".", "-").replace("_", "-").split("-") if part.isalpha()]
        if words:
            return " ".join(words)
    if norm.account_identifier:
        return f"A/C {norm.account_identifier}"
    return "Unknown counterparty"


def create_person_from_transaction(
    db: Session, transaction: Transaction, norm: NormalizedTransaction
) -> Person:
    """Create a person from a STRONG identifier only (never from a bare name)."""
    canonical = _derive_canonical_name(norm)
    normalized = normalize_name(canonical) or canonical.lower()

    person: Person | None = None
    for attempt in range(25):
        count = db.query(Person).count()
        code = f"P{count + 1 + attempt:03d}"
        if db.query(Person).filter(Person.code == code).first() is not None:
            continue
        person = Person(
            code=code,
            canonical_name=canonical,
            normalized_name=normalized,
            source=PersonSource.AUTO,
            first_seen_date=transaction.transaction_date,
            last_seen_date=transaction.transaction_date,
        )
        db.add(person)
        try:
            db.flush()
            break
        except IntegrityError:
            db.rollback()
            person = None
    if person is None:  # pragma: no cover - extreme collision only
        raise RuntimeError("Could not allocate a person code.")

    def add_identifier(id_type: str, value: str | None, display: str | None = None) -> None:
        if not value:
            return
        existing = (
            db.query(PersonIdentifier)
            .filter(
                PersonIdentifier.person_id == person.id,
                PersonIdentifier.id_type == id_type,
                PersonIdentifier.normalized_value == value,
            )
            .first()
        )
        if existing is None:
            db.add(
                PersonIdentifier(
                    person_id=person.id,
                    id_type=id_type,
                    value=display or value,
                    normalized_value=value,
                    source="statement",
                )
            )

    add_identifier(IdentifierType.UPI, norm.normalized_upi_id)
    add_identifier(IdentifierType.ACCOUNT, norm.account_identifier)
    add_identifier(
        IdentifierType.NAME, norm.normalized_name, display=norm.name
    )
    add_identifier(IdentifierType.PHONE, norm.phone)
    add_identifier(IdentifierType.EMAIL, norm.email)
    db.flush()
    return person


def attach_alias(db: Session, person: Person, norm: NormalizedTransaction) -> None:
    """Record a new name form for an already-matched person (alias, not merge)."""
    if not norm.normalized_name:
        return
    exists = any(
        identifier.id_type == IdentifierType.NAME
        and identifier.normalized_value == norm.normalized_name
        for identifier in person.identifiers
    )
    if not exists:
        db.add(
            PersonIdentifier(
                person_id=person.id,
                id_type=IdentifierType.NAME,
                value=norm.name or norm.normalized_name,
                normalized_value=norm.normalized_name,
                source="statement",
            )
        )


def _serializable_candidates(results: list[CandidateResult]) -> list[dict]:
    return [
        {
            "person_id": result.person.code,
            "name": result.person.canonical_name,
            "score": result.score,
            "evidence": [
                {
                    "code": item.code,
                    "weight": item.weight,
                    "strength": item.strength,
                    "detail": item.detail,
                }
                for item in result.evidence
            ],
        }
        for result in results
    ]


def _serializable_evidence(outcome: MatchOutcome) -> dict:
    evidence: dict = {"local": [], "jev": outcome.jev_raw}
    for result in outcome.candidates:
        for item in result.evidence:
            evidence["local"].append(
                {
                    "code": item.code,
                    "weight": item.weight,
                    "strength": item.strength,
                    "detail": item.detail,
                }
            )
    if outcome.status == MatchingStatus.MATCH and outcome.person:
        # Evidence for the chosen person (list may be empty for NEW_PERSON).
        pass
    return evidence


def _attach_unowned_identifiers(
    db: Session, person: Person, norm: NormalizedTransaction
) -> None:
    """Store strong identifiers observed on a MATCHED transaction.

    Identifiers already owned by another person are left alone (no merging).
    """
    for id_type, value in (
        (IdentifierType.UPI, norm.normalized_upi_id),
        (IdentifierType.ACCOUNT, norm.account_identifier),
        (IdentifierType.PHONE, norm.phone),
        (IdentifierType.EMAIL, norm.email),
        (IdentifierType.REFERENCE, norm.reference),
    ):
        if not value:
            continue
        owned = (
            db.query(PersonIdentifier)
            .filter(
                PersonIdentifier.id_type == id_type,
                PersonIdentifier.normalized_value == value,
            )
            .first()
        )
        if owned is not None:
            continue
        db.add(
            PersonIdentifier(
                person_id=person.id,
                id_type=id_type,
                value=value,
                normalized_value=value,
                source="statement",
            )
        )


def apply_outcome(
    db: Session,
    transaction: Transaction,
    outcome: MatchOutcome,
    norm: NormalizedTransaction,
) -> None:
    """Persist the automatic decision on the transaction and its match row."""
    person = outcome.person if outcome.status == MatchingStatus.MATCH else None

    transaction.counterparty_id = person.id if person else None
    transaction.matching_status = outcome.status
    transaction.matching_method = outcome.method
    transaction.matching_confidence = outcome.confidence
    transaction.matching_reason = outcome.reason

    match = transaction.match
    if match is None:
        match = TransactionPersonMatch(transaction=transaction)
        db.add(match)

    match.person_id = person.id if person else None
    match.suggested_person_id = (
        outcome.suggested_person.id if outcome.suggested_person else None
    )
    match.status = outcome.status
    match.method = outcome.method
    match.confidence = outcome.confidence
    match.reason_codes = list(outcome.reason_codes)
    match.reason = outcome.reason
    match.candidates = _serializable_candidates(outcome.candidates)
    match.evidence = _serializable_evidence(outcome)
    match.needs_review = outcome.needs_review or outcome.status != MatchingStatus.MATCH
    match.reviewed_by = None
    match.reviewed_at = None

    if person is not None:
        attach_alias(db, person, norm)
        _attach_unowned_identifiers(db, person, norm)
        if transaction.transaction_date:
            if person.first_seen_date is None or transaction.transaction_date < person.first_seen_date:
                person.first_seen_date = transaction.transaction_date
            if person.last_seen_date is None or transaction.transaction_date > person.last_seen_date:
                person.last_seen_date = transaction.transaction_date

    db.flush()


def match_transaction(
    db: Session,
    settings: Settings,
    transaction: Transaction,
    norm: NormalizedTransaction,
) -> MatchOutcome:
    """Full automatic matching flow for one transaction. Commits at the end."""
    hits = search_candidates(db, norm)
    people = [hit.person for hit in hits]
    context = MatchContext(person_methods=load_person_methods(db, [p.id for p in people]))

    results = evaluate_candidates(norm, people, context)
    local_outcome = decide(norm, results)

    has_strong = norm.has_strong_identifier
    found_strong = any(
        item.code in _STRONG_CODES for result in results for item in result.evidence
    )
    best_score = results[0].score if results else 0.0

    # Create a person only when a strong identifier exists AND no candidate
    # reached medium evidence: a medium name match must not be fragmented by
    # minting a duplicate person for a new UPI/account.
    if has_strong and not found_strong and best_score < 0.55:
        # The strong identifier belongs to nobody yet -> create the person.
        person = create_person_from_transaction(db, transaction, norm)
        created_result = CandidateResult(
            person=person,
            evidence=[
                EvidenceItem(
                    "NEW_PERSON_STRONG_IDENTIFIER",
                    0.9,
                    "strong",
                    f"Created {person.code} from strong identifier "
                    f"({norm.normalized_upi_id or norm.account_identifier})",
                )
            ],
            score=0.9,
        )
        results = [created_result, *results]
        local_outcome = decide(norm, results)

    outcome = jev_resolve(settings, norm, results, local_outcome, transaction)
    apply_outcome(db, transaction, outcome, norm)
    db.commit()
    return outcome
