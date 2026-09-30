"""Evidence rules for person/counterparty matching.

Strong identifiers (exact UPI/account/reference) always outweigh medium
evidence (partial UPI, full name, name + payment method), which outweighs
weak evidence (partial/generic name alone). Weights are deterministic and
every item is reported so decisions stay explainable.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import PaymentMethod
from app.models.person import Person, PersonIdentifier
from app.services.normalization import NormalizedTransaction


@dataclass
class EvidenceItem:
    code: str
    weight: float
    strength: str  # strong | medium | weak
    detail: str


@dataclass
class MatchContext:
    """Pre-loaded context so evidence rules never hit the database."""

    person_methods: dict[int, set[str]]


def _person_values(person: Person, id_type: str) -> list[str]:
    return [
        identifier.normalized_value
        for identifier in person.identifiers
        if identifier.id_type == id_type and identifier.normalized_value
    ]


def _upi_local(upi: str) -> str:
    return upi.split("@", 1)[0]


def compare(
    norm: NormalizedTransaction,
    person: Person,
    context: MatchContext | None = None,
) -> list[EvidenceItem]:
    """Return every piece of evidence linking this transaction to `person`."""
    items: list[EvidenceItem] = []

    txn_upis = [norm.normalized_upi_id] if norm.normalized_upi_id else []
    person_upis = _person_values(person, "UPI")

    # --- Strong: exact identifiers ------------------------------------------
    for txn_upi in txn_upis:
        if txn_upi in person_upis:
            items.append(
                EvidenceItem(
                    "UPI_EXACT_MATCH",
                    0.95,
                    "strong",
                    f"UPI id {txn_upi} belongs to {person.code}",
                )
            )

    txn_account = norm.account_identifier
    if txn_account:
        person_accounts = _person_values(person, "ACCOUNT")
        if txn_account in person_accounts:
            items.append(
                EvidenceItem(
                    "ACCOUNT_EXACT_MATCH",
                    0.9,
                    "strong",
                    f"Account {txn_account} belongs to {person.code}",
                )
            )

    if norm.phone and norm.phone in _person_values(person, "PHONE"):
        items.append(
            EvidenceItem("PHONE_EXACT_MATCH", 0.85, "strong", "Phone number matches")
        )
    if norm.email and norm.email in _person_values(person, "EMAIL"):
        items.append(
            EvidenceItem("EMAIL_EXACT_MATCH", 0.85, "strong", "Email address matches")
        )
    if norm.reference and norm.reference in _person_values(person, "REFERENCE"):
        items.append(
            EvidenceItem(
                "REFERENCE_MATCH", 0.8, "strong", "Transaction reference matches"
            )
        )

    # --- Medium: partial identifiers ----------------------------------------
    for txn_upi in txn_upis:
        if any(item.code == "UPI_EXACT_MATCH" for item in items):
            break
        txn_handle = _upi_local(txn_upi)
        txn_domain = txn_upi.split("@", 1)[1]
        partial_hit = False
        domain_hit = False
        for person_upi in person_upis:
            person_handle = _upi_local(person_upi)
            person_domain = person_upi.split("@", 1)[1]
            shorter, longer = sorted(
                (txn_handle, person_handle), key=len
            )
            containment = (
                longer.startswith(shorter)
                or shorter.startswith(longer)
                or shorter in longer
                or longer in shorter
            )
            # A shared prefix only counts when it is a substantial part of
            # both handles: "ravi" is NOT a partial match for "ravipavan"
            # (different people), but "kalyan" ~ "kalyan@oksbi" is.
            if (
                len(shorter) >= 5
                and len(longer) >= 5
                and len(shorter) / len(longer) >= 0.5
                and containment
            ):
                items.append(
                    EvidenceItem(
                        "UPI_PARTIAL_MATCH",
                        0.55,
                        "medium",
                        f"Partial UPI match: {txn_upi} ~ {person_upi}",
                    )
                )
                partial_hit = True
                break
            if txn_domain == person_domain and not partial_hit:
                domain_hit = True
        if not partial_hit and domain_hit:
            items.append(
                EvidenceItem(
                    "UPI_DOMAIN_MATCH",
                    0.15,
                    "weak",
                    f"Same UPI domain @{txn_domain} only",
                )
            )

    # --- Name evidence --------------------------------------------------------
    person_tokens = {
        token for token in (person.normalized_name or "").split() if token
    }
    txn_tokens = {token.lower() for token in norm.name_tokens}
    shared = person_tokens & txn_tokens

    name_weight = 0.0
    if norm.normalized_name and norm.normalized_name == person.normalized_name:
        items.append(
            EvidenceItem(
                "NAME_EXACT_MATCH",
                0.75,
                "medium",
                "Full counterparty name matches",
            )
        )
        name_weight = 0.75
    elif shared:
        shorter = min(len(person_tokens), len(txn_tokens))
        if len(shared) >= 2 and len(shared) / max(shorter, 1) >= 0.5:
            items.append(
                EvidenceItem(
                    "NAME_PARTIAL_MATCH",
                    0.6,
                    "medium",
                    f"Name tokens overlap: {', '.join(sorted(shared))}",
                )
            )
            name_weight = 0.6
        else:
            items.append(
                EvidenceItem(
                    "NAME_SINGLE_TOKEN_MATCH",
                    0.3,
                    "weak",
                    f"Only partial name token matches: {', '.join(sorted(shared))}",
                )
            )
            name_weight = 0.3

        # Name + payment method (same method seen on this person's history).
        if (
            context
            and norm.payment_method
            and norm.payment_method != PaymentMethod.OTHER
            and norm.payment_method in context.person_methods.get(person.id, set())
        ):
            items.append(
                EvidenceItem(
                    "NAME_PLUS_PAYMENT_METHOD",
                    0.5,
                    "medium",
                    f"Name matches and {norm.payment_method} was used before",
                )
            )

    return items


def evidence_score(items: list[EvidenceItem]) -> float:
    """Deterministic confidence: strongest item plus a small corroboration bump."""
    if not items:
        return 0.0
    base = max(item.weight for item in items)
    corroborating = sum(1 for item in items if item.weight >= 0.5)
    bonus = min(0.04, 0.02 * max(corroborating - 1, 0))
    return round(min(0.99, base + bonus), 4)
