"""Response serializers for records with JSON blobs."""

from __future__ import annotations

from app.models import TransactionPersonMatch
from app.schemas.match import CandidateEvidence, MatchOut


def _person_code(match: TransactionPersonMatch, attribute: str) -> str | None:
    person = getattr(match, attribute)
    return person.code if person is not None else None


def serialize_match(match: TransactionPersonMatch | None) -> MatchOut | None:
    if match is None:
        return None
    raw_evidence = match.evidence or {}
    if isinstance(raw_evidence, dict):
        local = raw_evidence.get("local") or []
    elif isinstance(raw_evidence, list):
        local = raw_evidence
    else:
        local = []

    return MatchOut(
        id=match.id,
        transaction_id=match.transaction_id,
        person_id=_person_code(match, "person"),
        suggested_person_id=_person_code(match, "suggested_person"),
        status=match.status,
        method=match.method,
        confidence=match.confidence,
        reason_codes=list(match.reason_codes or []),
        reason=match.reason,
        candidates=list(match.candidates or []),
        evidence=[item for item in local if isinstance(item, dict)],
        needs_review=bool(match.needs_review),
        reviewed_by=match.reviewed_by,
        reviewed_at=match.reviewed_at,
    )


def evidence_list(value) -> list[CandidateEvidence]:
    if isinstance(value, dict):
        value = value.get("local") or []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
