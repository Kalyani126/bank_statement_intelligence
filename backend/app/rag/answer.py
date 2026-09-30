"""Answer composition.

Financial numbers are NEVER computed here or by an LLM - only SQL/Python
results are phrased. When no LLM is configured, deterministic templates
produce the answer and are labelled explanation_source = TEMPLATE.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings
from app.rag.context_builder import QueryContext, format_inr, supporting_transactions

logger = logging.getLogger(__name__)

INSUFFICIENT = (
    "I couldn't determine this confidently from the available statement data."
)
AMBIGUOUS = "Multiple possible matches were found. Please review."


@dataclass
class AnswerResult:
    text: str
    answer_source: str
    explanation_source: str  # TEMPLATE | LLM
    aggregates: dict[str, Any] = field(default_factory=dict)
    supporting: list[dict] = field(default_factory=list)
    person_candidates: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _jsonable(value: Any) -> Any:
    """Decimal/datetime -> string; containers recursed. Never loses precision."""
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _serializable_aggregates(aggregates: dict[str, Any] | None) -> dict[str, Any]:
    if not aggregates:
        return {}
    return {key: _jsonable(value) for key, value in aggregates.items()}


_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _period_label(profile) -> str:
    """Human period for an answer, built only from parsed question text."""
    if profile.month and 1 <= profile.month <= 12:
        name = _MONTH_NAMES[profile.month - 1]
        return f" in {name} {profile.year}" if profile.year else f" in {name}"
    if profile.year:
        return f" in {profile.year}"
    return ""


def _ambiguous_text(context: QueryContext) -> str:
    label = context.person_label or "several possible counterparties"
    return f"{AMBIGUOUS} Candidates: {label}."


def compose_template(context: QueryContext) -> str:
    profile = context.profile
    notes = context.notes
    aggregates = context.aggregates or {}
    has_rows = bool(context.rows)

    if "MULTIPLE_PERSON_MATCHES" in notes or "MULTIPLE_POSSIBLE_MATCHES" in notes:
        return _ambiguous_text(context)

    label = context.person_label or "counterparties"
    method = profile.payment_method.replace("_", " ").title() if profile.payment_method else None

    if profile.intent in ("total_sent", "total_received"):
        if not has_rows:
            return INSUFFICIENT
        word = "sent" if profile.intent == "total_sent" else "received"
        total = aggregates.get("total_debit" if word == "sent" else "total_credit")
        if total is None:
            return INSUFFICIENT
        count = aggregates.get("count", len(context.rows))
        return (
            f"{label} — Total {word}: Rs {format_inr(total)}. "
            f"(Verified from {count} transaction{'s' if count != 1 else ''} in PostgreSQL.)"
        )

    if profile.intent == "count":
        if not has_rows:
            return INSUFFICIENT
        count = aggregates.get("count")
        if count is None:
            return INSUFFICIENT
        parts = [f"{count}"]
        if method:
            parts.append(method)
        parts.append("transaction" if count == 1 else "transactions")
        sentence = " ".join(parts)
        if profile.person_text:
            sentence += f" with {label}"
        return sentence + "."

    if profile.intent == "largest":
        if not has_rows:
            return INSUFFICIENT
        largest = aggregates.get("largest_debit")
        if largest is None:
            return INSUFFICIENT
        match = next(
            (
                row
                for row in context.rows
                if row.debit_amount is not None and str(row.debit_amount) == str(largest)
            ),
            None,
        )
        when = f" on {match.transaction_date}" if match and match.transaction_date else ""
        return f"Largest debit{when}: Rs {format_inr(largest)}."

    if profile.intent == "overall_totals":
        if not has_rows:
            return INSUFFICIENT
        count = aggregates.get("count", len(context.rows))
        return (
            f"Total debits: Rs {format_inr(aggregates.get('total_debit'))} · "
            f"Total credits: Rs {format_inr(aggregates.get('total_credit'))} "
            f"(verified from {count} transaction{'s' if count != 1 else ''} "
            f"in PostgreSQL{_period_label(profile)})."
        )

    if profile.intent == "top_counterparty":
        tops = (aggregates.get("counterparties") or {}).get("top") or []
        if not has_rows or not tops:
            return INSUFFICIENT
        lead = tops[0]
        incoming = profile.direction == "CREDIT"
        total = lead.get("total_credit") if incoming else lead.get("total_debit")
        word = "received" if incoming else "sent"
        name = lead.get("name") or "an unnamed counterparty"
        if lead.get("code"):
            name = f"{name} ({lead['code']})"
        count = int(lead.get("count") or 0)
        return (
            f"Most money {word} to {name}: Rs {format_inr(total)} across "
            f"{count} transaction{'s' if count != 1 else ''}."
        )

    if profile.intent == "counterparties":
        breakdown = aggregates.get("counterparties") or {}
        tops = breakdown.get("top") or []
        if not tops:
            return INSUFFICIENT
        distinct = int(breakdown.get("distinct") or len(tops))
        parts = []
        for entry in tops[:5]:
            name = entry.get("name") or "Unknown"
            if entry.get("code"):
                name = f"{name} ({entry['code']})"
            count = int(entry.get("count") or 0)
            parts.append(f"{name} — {count} transaction{'s' if count != 1 else ''}")
        return (
            f"You transacted with {distinct} "
            f"counterpart{'y' if distinct == 1 else 'ies'}{_period_label(profile)}. "
            f"Top: {'; '.join(parts)}."
        )

    if profile.intent == "balance":
        if not has_rows or aggregates.get("closing_balance") is None:
            return INSUFFICIENT
        when = aggregates.get("balance_date")
        return (
            f"Closing balance{f' on {when}' if when else ''}: "
            f"Rs {format_inr(aggregates.get('closing_balance'))}."
        )

    # list / generic
    if not has_rows:
        return INSUFFICIENT
    count = aggregates.get("count", len(context.rows))
    period = _period_label(profile)
    if profile.person_text:
        return f"Found {count} transaction{'s' if count != 1 else ''} with {label}{period}."
    return f"Found {count} transaction{'s' if count != 1 else ''}{period}."


def _verified_payload(context: QueryContext) -> dict[str, Any]:
    aggregates = context.aggregates or {}
    return {
        "question": context.question,
        "person": context.person_label,
        "filters": {
            "payment_method": context.profile.payment_method,
            "direction": context.profile.direction,
            "month": context.profile.month,
            "year": context.profile.year,
        },
        "verified_sql_aggregates": {
            "count": aggregates.get("count"),
            "total_debit_rs": format_inr(aggregates.get("total_debit")),
            "total_credit_rs": format_inr(aggregates.get("total_credit")),
            "largest_debit_rs": format_inr(aggregates.get("largest_debit")),
            "first_date": aggregates.get("first_date"),
            "last_date": aggregates.get("last_date"),
        },
        "supporting_transactions": supporting_transactions(context.rows, limit=15),
        "data_sufficiency": "ambiguous" if context.notes else "ok",
    }


def compose_llm(
    settings: Settings, context: QueryContext, template_answer: str
) -> tuple[str, str]:
    """Phrase verified results with the configured LLM (backend-only key)."""
    system = (
        "You explain verified bank statement query results. You must NOT do "
        "arithmetic, and you must NOT invent any transaction, amount, date, "
        "UPI id, account number, person or reference. Repeat numbers exactly "
        "as given in the verified payload. If the verified payload says the "
        "data is ambiguous, reply exactly: 'Multiple possible matches were "
        "found. Please review.' If there is no verified data, reply exactly: "
        "'I couldn't determine this confidently from the available statement "
        "data.' Keep the answer to 1-3 sentences."
    )
    user = json.dumps(_verified_payload(context), ensure_ascii=False, default=str)
    try:
        response = httpx.post(
            f"{settings.openai_base_url}/chat/completions",
            json={
                "model": settings.openai_model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=30.0,
        )
        response.raise_for_status()
        body = response.json()
        text = body["choices"][0]["message"]["content"].strip()
        if not text:
            raise ValueError("empty completion")
        return text, "LLM"
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as error:
        logger.warning("LLM answer failed (%s); using verified template.", type(error).__name__)
        return template_answer, "TEMPLATE"


def compose_answer(settings: Settings, context: QueryContext) -> AnswerResult:
    template = compose_template(context)
    explanation_source = "TEMPLATE"
    text = template
    if settings.llm_configured:
        text, explanation_source = compose_llm(settings, context, template)

    aggregates = context.aggregates or {}
    return AnswerResult(
        text=text,
        answer_source=context.mode,
        explanation_source=explanation_source,
        aggregates=_serializable_aggregates(aggregates),
        supporting=supporting_transactions(context.rows),
        person_candidates=[
            {"code": person.code, "name": person.canonical_name}
            for person in context.retrieval.person_resolution.persons
        ],
        notes=context.notes,
    )
