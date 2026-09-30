"""Builds typed System One questions for JEV and parses its decisions.

One question per field (TypeSafe's guidance): a MATCH/AMBIGUOUS/UNKNOWN
choice, a candidate-person choice, and the strongest-evidence choice.
"""

from __future__ import annotations

import json
from typing import Any

from app.jev.client import JEVInvalidResponseError
from app.jev.schemas import JEVDecision, JEVDecisionRequest

_VALID_STATUSES = {"MATCH", "AMBIGUOUS", "UNKNOWN"}

_REASON_CODE_DESCRIPTIONS: dict[str, str] = {
    "UPI_EXACT_MATCH": "The exact UPI id on the transaction belongs to the candidate.",
    "ACCOUNT_EXACT_MATCH": "The masked account number on the transaction belongs to the candidate.",
    "PHONE_EXACT_MATCH": "The phone number belongs to the candidate.",
    "EMAIL_EXACT_MATCH": "The email address belongs to the candidate.",
    "REFERENCE_MATCH": "The transaction reference belongs to the candidate.",
    "UPI_PARTIAL_MATCH": "Only part of the UPI handle matches the candidate.",
    "UPI_DOMAIN_MATCH": "Only the UPI bank domain matches - weak evidence.",
    "ACCOUNT_PARTIAL_MATCH": "Only part of the account number matches.",
    "NAME_EXACT_MATCH": "The full counterparty name matches the candidate.",
    "NAME_PARTIAL_MATCH": "Only part of the counterparty name matches.",
    "NAME_SINGLE_TOKEN_MATCH": "Only one generic name token matches.",
    "NAME_PLUS_PAYMENT_METHOD": "Name matches and the same payment method was used before.",
    "NEW_PERSON_STRONG_IDENTIFIER": "A new person was created from a strong identifier.",
    "INSUFFICIENT_EVIDENCE": "Evidence is too weak to decide.",
    "NO_CANDIDATES": "No candidate people exist in the database.",
}


def build_state(request: JEVDecisionRequest) -> str:
    return json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def build_questions(
    request: JEVDecisionRequest, reason_codes: list[str]
) -> dict[str, Any]:
    criteria: dict[str, str] = {
        "MATCH": (
            "Exactly one candidate is identified by strong evidence (exact UPI id, "
            "exact account number, exact reference) or an unambiguous full-name match."
        ),
        "AMBIGUOUS": (
            "Evidence points to more than one candidate, or only weak/partial "
            "evidence exists (name fragment, shared UPI domain)."
        ),
        "UNKNOWN": "No candidate is plausibly related to this transaction.",
    }

    questions: dict[str, Any] = {
        "match_decision": {
            "type": "choice",
            "instructions": (
                "Decide if this bank transaction can be attributed to exactly one "
                "of the candidate people. Do not guess on weak evidence."
            ),
            "criteria": criteria,
        }
    }

    if request.candidates:
        questions["matched_person"] = {
            "type": "choice",
            "instructions": (
                "If match_decision is MATCH, name the one candidate person that "
                "owns this transaction; otherwise choose none."
            ),
            "criteria": {
                **{
                    candidate.person_id: (
                        f"{candidate.name} | UPI: "
                        f"{', '.join(candidate.upi_ids) or '-'} | A/C: "
                        f"{', '.join(candidate.account_identifiers) or '-'} | "
                        f"aliases: {', '.join(candidate.aliases) or '-'}"
                    )
                    for candidate in request.candidates
                },
                "none": "No candidate matches this transaction.",
            },
        }

    usable_codes = [
        code for code in reason_codes if code in _REASON_CODE_DESCRIPTIONS
    ] or ["INSUFFICIENT_EVIDENCE"]
    questions["primary_reason"] = {
        "type": "choice",
        "instructions": "The single strongest piece of evidence behind the decision.",
        "criteria": {code: _REASON_CODE_DESCRIPTIONS[code] for code in usable_codes},
    }
    return questions


def parse_decision(response: dict[str, Any], allowed_person_ids: set[str]) -> JEVDecision:
    answers = response.get("answers")
    if not isinstance(answers, dict):
        raise JEVInvalidResponseError("JEV response has no answers object.")

    decision_answer = answers.get("match_decision") or {}
    status = decision_answer.get("choice")
    if status not in _VALID_STATUSES:
        raise JEVInvalidResponseError(f"JEV returned an unknown decision: {status!r}")

    confidence = decision_answer.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        confidence = max(0.0, min(1.0, float(confidence)))
    else:
        confidence = None

    person_answer = answers.get("matched_person") or {}
    person_choice = person_answer.get("choice")
    person_id = person_choice if person_choice in allowed_person_ids else None

    reason_codes: list[str] = []
    reason_answer = answers.get("primary_reason") or {}
    reason_choice = reason_answer.get("choice")
    if isinstance(reason_choice, str):
        reason_codes.append(reason_choice)

    if status == "MATCH" and person_id is None:
        # Never invent a person: degrade to AMBIGUOUS for manual review.
        status = "AMBIGUOUS"
        reason_codes.append("JEV_MATCH_WITHOUT_VALID_PERSON")
    elif status != "MATCH":
        person_id = None

    return JEVDecision(
        status=status,  # type: ignore[arg-type]
        person_id=person_id,
        reason_codes=reason_codes,
        confidence=confidence,
        model=response.get("model"),
        raw=response,
    )
