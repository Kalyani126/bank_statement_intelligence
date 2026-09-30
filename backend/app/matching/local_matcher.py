"""Deterministic local matching (used when JEV is disabled or unavailable).

Never labelled as JEV - results carry matching_method = LOCAL_MATCHING.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.matching.evidence import (
    EvidenceItem,
    MatchContext,
    compare,
    evidence_score,
)
from app.models.enums import MatchingStatus
from app.models.person import Person
from app.services.normalization import NormalizedTransaction

# Thresholds for the deterministic layer.
_STRONG = 0.8
_MEDIUM = 0.55


@dataclass
class CandidateResult:
    person: Person
    evidence: list[EvidenceItem]
    score: float


@dataclass
class MatchOutcome:
    status: MatchingStatus
    person: Person | None = None
    suggested_person: Person | None = None
    confidence: float | None = None
    reason_codes: list[str] = field(default_factory=list)
    reason: str = ""
    candidates: list[CandidateResult] = field(default_factory=list)
    needs_review: bool = False
    # Filled in by the JEV service when JEV actually answered.
    method: str = "LOCAL_MATCHING"
    jev_raw: dict | None = None
    jev_model: str | None = None


def evaluate_candidates(
    norm: NormalizedTransaction,
    people: list[Person],
    context: MatchContext | None,
) -> list[CandidateResult]:
    results: list[CandidateResult] = []
    for person in people:
        items = compare(norm, person, context)
        if not items:
            continue
        results.append(
            CandidateResult(person=person, evidence=items, score=evidence_score(items))
        )
    results.sort(key=lambda result: (-result.score, result.person.id))
    return results


def _is_generic_name(norm: NormalizedTransaction) -> bool:
    """A single short token like 'RAVI' or 'KALYAN' cannot identify anyone."""
    return len(norm.name_tokens) == 1


def decide(norm: NormalizedTransaction, results: list[CandidateResult]) -> MatchOutcome:
    """MATCH / AMBIGUOUS / UNKNOWN from scored candidates. Never guesses."""
    if not results:
        return MatchOutcome(
            status=MatchingStatus.UNKNOWN,
            reason="No candidate people found in the database.",
            reason_codes=["NO_CANDIDATES"],
        )

    best = results[0]
    second = results[1] if len(results) > 1 else None

    if best.score >= _STRONG:
        if second and second.score >= _STRONG and (best.score - second.score) <= 0.05:
            return _ambiguous(norm, results, ["STRONG_IDENTIFIER_SHARED_BY_MULTIPLE"])
        return _match(norm, best)

    if best.score >= _MEDIUM:
        if second and (best.score - second.score) < 0.1:
            return _ambiguous(norm, results, ["CLOSE_CALL_BETWEEN_CANDIDATES"])
        return _match(norm, best)

    # Weak evidence only: never silently assign.
    codes = ["INSUFFICIENT_EVIDENCE"]
    if _is_generic_name(norm):
        if len(results) > 1:
            codes.append("GENERIC_NAME_MULTIPLE_CANDIDATES")
        else:
            codes.append("GENERIC_NAME_ONLY")
    return _ambiguous(norm, results, codes)


def _match(norm: NormalizedTransaction, best: CandidateResult) -> MatchOutcome:
    return MatchOutcome(
        status=MatchingStatus.MATCH,
        person=best.person,
        suggested_person=best.person,
        confidence=best.score,
        reason_codes=[item.code for item in best.evidence],
        reason="; ".join(item.detail for item in best.evidence),
        candidates=[best],
        needs_review=False,
    )


def _ambiguous(
    norm: NormalizedTransaction,
    results: list[CandidateResult],
    extra_codes: list[str],
) -> MatchOutcome:
    codes: list[str] = []
    for result in results[:2]:
        for item in result.evidence:
            if item.code not in codes:
                codes.append(item.code)
    codes.extend(code for code in extra_codes if code not in codes)

    top = results[0]
    return MatchOutcome(
        status=MatchingStatus.AMBIGUOUS,
        person=None,
        suggested_person=top.person,
        confidence=top.score,
        reason_codes=codes,
        reason=(
            f"{len(results)} candidate(s) found; strongest is {top.person.code} "
            f"({top.person.canonical_name}) at score {top.score:.2f}. "
            "Manual review required."
        ),
        candidates=results,
        needs_review=True,
    )
