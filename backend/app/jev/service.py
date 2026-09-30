"""JEV orchestration.

Rules enforced here:
- JEV_ENABLED=false  -> never call TypeSafe; LOCAL_MATCHING is used and labelled.
- JEV_ENABLED=true with credentials -> the real TypeSafe System One API is
  called; only then is matching_method recorded as JEV.
- JEV_ENABLED=true without credentials or when the API fails -> NO fake JEV
  result. The local suggestion is kept, the transaction is flagged for manual
  review, and the reason codes record why JEV could not decide.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.jev.client import JEVError, JEVNotConfiguredError, TypeSafeJEVClient
from app.jev.matcher import build_questions, build_state, parse_decision
from app.jev.schemas import JEVDecisionRequest, JEVCandidate, JEVTransaction
from app.matching.local_matcher import CandidateResult, MatchOutcome
from app.models.enums import MatchingMethod, MatchingStatus
from app.models.transaction import Transaction
from app.services.normalization import NormalizedTransaction

logger = logging.getLogger(__name__)


def _build_request(
    norm: NormalizedTransaction,
    results: list[CandidateResult],
    transaction: Transaction | None,
) -> JEVDecisionRequest:
    txn = JEVTransaction(
        raw_description=norm.raw_description,
        normalized_name=norm.normalized_name,
        name_tokens=norm.name_tokens,
        normalized_upi=norm.normalized_upi_id,
        account_identifier=norm.account_identifier,
        reference=norm.reference,
        payment_method=norm.payment_method.value if norm.payment_method else None,
        transaction_date=str(transaction.transaction_date) if transaction and transaction.transaction_date else None,
        direction=transaction.transaction_type if transaction else None,
        amount=str(transaction.debit_amount or transaction.credit_amount) if transaction else None,
    )
    candidates = [
        JEVCandidate(
            person_id=result.person.code,
            name=result.person.canonical_name,
            aliases=[
                identifier.value
                for identifier in result.person.identifiers
                if identifier.id_type == "NAME"
            ],
            upi_ids=[
                identifier.value
                for identifier in result.person.identifiers
                if identifier.id_type == "UPI"
            ],
            account_identifiers=[
                identifier.value
                for identifier in result.person.identifiers
                if identifier.id_type == "ACCOUNT"
            ],
            phone_numbers=[
                identifier.value
                for identifier in result.person.identifiers
                if identifier.id_type == "PHONE"
            ],
            email_addresses=[
                identifier.value
                for identifier in result.person.identifiers
                if identifier.id_type == "EMAIL"
            ],
            local_evidence=[item.code for item in result.evidence],
            local_score=result.score,
        )
        for result in results
    ]
    return JEVDecisionRequest(transaction=txn, candidates=candidates)


def _fallback(local_outcome: MatchOutcome, code: str, message: str) -> MatchOutcome:
    """JEV could not decide: keep the local suggestion, force manual review."""
    outcome = MatchOutcome(
        status=local_outcome.status,
        person=local_outcome.person,
        suggested_person=local_outcome.suggested_person or local_outcome.person,
        confidence=local_outcome.confidence,
        reason_codes=[*local_outcome.reason_codes, code],
        reason=f"{local_outcome.reason} {message}".strip(),
        candidates=local_outcome.candidates,
        needs_review=True,
        method=MatchingMethod.LOCAL_MATCHING,
    )
    if outcome.status == MatchingStatus.MATCH:
        # A JEV-enabled deployment must not auto-assign without JEV having
        # actually decided - demote to review with the local suggestion.
        outcome.suggested_person = local_outcome.person
        outcome.person = None
        outcome.status = MatchingStatus.AMBIGUOUS
    return outcome


def resolve(
    settings: Settings,
    norm: NormalizedTransaction,
    results: list[CandidateResult],
    local_outcome: MatchOutcome,
    transaction: Transaction | None = None,
) -> MatchOutcome:
    """Return the final automatic outcome: JEV, or labelled local matching."""
    if not settings.jev_enabled:
        return local_outcome  # method stays LOCAL_MATCHING - never called JEV

    if not settings.jev_configured:
        logger.error(
            "JEV_ENABLED is true but TypeSafe credentials are missing - "
            "falling back to manual review (no fake JEV)."
        )
        return _fallback(
            local_outcome,
            "JEV_NOT_CONFIGURED",
            "JEV is enabled but TYPESAFE_API_KEY/TYPESAFE_API_BASE are not configured.",
        )

    if not results:
        # Nothing for JEV to decide (no candidate people).
        return local_outcome

    request = _build_request(norm, results, transaction)
    reason_pool: list[str] = []
    for result in results:
        for item in result.evidence:
            if item.code not in reason_pool:
                reason_pool.append(item.code)
    if not reason_pool:
        reason_pool.append("INSUFFICIENT_EVIDENCE")

    client = TypeSafeJEVClient(
        api_key=settings.typesafe_api_key,
        base_url=settings.typesafe_api_base,
        model=settings.jev_model,
        timeout=settings.jev_timeout_seconds,
    )

    try:
        response = client.evaluate(
            state=build_state(request),
            questions=build_questions(request, reason_pool),
        )
        decision = parse_decision(
            response, {result.person.code for result in results}
        )
    except JEVNotConfiguredError as error:
        logger.error("JEV credentials rejected: %s", error)
        return _fallback(local_outcome, "JEV_NOT_CONFIGURED", str(error))
    except JEVError as error:
        logger.warning("JEV call failed: %s", error)
        return _fallback(local_outcome, "JEV_UNAVAILABLE", str(error))

    chosen = next(
        (
            result
            for result in results
            if decision.person_id and result.person.code == decision.person_id
        ),
        None,
    )
    top = results[0]

    if decision.status == "MATCH" and chosen is not None:
        codes = list(decision.reason_codes)
        for item in chosen.evidence:
            if item.code not in codes:
                codes.append(item.code)
        return MatchOutcome(
            status=MatchingStatus.MATCH,
            person=chosen.person,
            suggested_person=chosen.person,
            confidence=decision.confidence,
            reason_codes=codes,
            reason=f"Decided by JEV ({decision.model or settings.jev_model}).",
            candidates=results,
            needs_review=False,
            method=MatchingMethod.JEV,
            jev_raw=decision.raw,
            jev_model=decision.model,
        )

    if decision.status == "AMBIGUOUS":
        suggested = chosen.person if chosen else (
            local_outcome.suggested_person or local_outcome.person or top.person
        )
        return MatchOutcome(
            status=MatchingStatus.AMBIGUOUS,
            person=None,
            suggested_person=suggested,
            confidence=decision.confidence,
            reason_codes=[*decision.reason_codes, "JEV_AMBIGUOUS"],
            reason=f"JEV reported ambiguity ({decision.model or settings.jev_model}).",
            candidates=results,
            needs_review=True,
            method=MatchingMethod.JEV,
            jev_raw=decision.raw,
            jev_model=decision.model,
        )

    # UNKNOWN
    codes = [*decision.reason_codes]
    if "NO_CANDIDATES" not in codes and not results:
        codes.append("NO_CANDIDATES")
    return MatchOutcome(
        status=MatchingStatus.UNKNOWN,
        person=None,
        suggested_person=local_outcome.suggested_person,
        confidence=decision.confidence,
        reason_codes=codes or ["JEV_UNKNOWN"],
        reason=f"JEV found no attributable person ({decision.model or settings.jev_model}).",
        candidates=results,
        needs_review=True,
        method=MatchingMethod.JEV,
        jev_raw=decision.raw,
        jev_model=decision.model,
    )
