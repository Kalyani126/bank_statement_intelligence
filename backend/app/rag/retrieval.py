"""Question understanding and retrieval.

RAG answers "which transactions are relevant to the question?" - identity is
NEVER decided here. Structured PostgreSQL filtering runs first; semantic
(vector) retrieval is only a fallback for fuzzy queries, always over the
user's own indexed statement rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from decimal import Decimal

from sqlalchemy import and_, func, literal, or_
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import IdentifierType, Person, PersonIdentifier, Statement, Transaction
from app.rag import embeddings as embedding_module
from app.rag import vector_store

_SENT_RE = re.compile(
    r"\b(send|sent|pay|paid|payout|gave|spent|spend|debit(?:ed)?)\b", re.I
)
_RECEIVED_RE = re.compile(
    r"\b(receive|received|got|refund|credit(?:ed)?)\b", re.I
)
_COUNT_RE = re.compile(r"\b(how many|count)\b", re.I)
_LARGEST_RE = re.compile(r"\b(biggest|largest|highest|maximum|max)\b", re.I)
_LIST_RE = re.compile(r"\b(show|list|find|display|give|which|what)\b", re.I)
_BALANCE_RE = re.compile(r"\b(balance|closing|ending)\b", re.I)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
# "Who/which person did I ... most/biggest ... to?" -> rank counterparties.
_TOP_COUNTERPARTY_RE = re.compile(
    r"(?:\b(?:who|whom)\b|\bwhich\s+(?:merchant|person|vendor|payee|recipient|account)\b)"
    r".*\b(most|biggest|largest|highest|top)\b",
    re.I | re.S,
)
# Relative periods ("last month", "this year") resolved against today.
_RELATIVE_RE = re.compile(r"\b(last|this|past|previous)\s+(month|year|week)\b", re.I)

# Semantic hits below this cosine score are treated as unrelated: a person
# question with no data must stay unanswered rather than show random rows.
_SEMANTIC_MIN_SCORE = 0.35

# "UPI" in a question means the rail, not just rows tagged UPI: Paytm,
# Google Pay and PhonePe are UPI apps (same for the bank-transfer rails).
_METHOD_FAMILY: dict[str, tuple[str, ...]] = {
    "UPI": ("UPI", "PAYTM", "GOOGLE_PAY", "PHONEPE"),
    "NEFT": ("NEFT", "IMPS", "RTGS", "BANK_TRANSFER"),
    "IMPS": ("IMPS", "NEFT", "RTGS", "BANK_TRANSFER"),
    "RTGS": ("RTGS", "NEFT", "IMPS", "BANK_TRANSFER"),
    "BANK_TRANSFER": ("BANK_TRANSFER", "NEFT", "IMPS", "RTGS"),
}

_MONTHS: dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_MONTH_RE = re.compile(
    r"\b("
    + "|".join(sorted(_MONTHS, key=len, reverse=True))
    + r")(?:\s+(\d{4}))?\b",
    re.I,
)

_METHOD_PHRASES: list[tuple[str, str]] = [
    ("google pay", "GOOGLE_PAY"),
    ("googlepay", "GOOGLE_PAY"),
    ("gpay", "GOOGLE_PAY"),
    ("phone pe", "PHONEPE"),
    ("phonepe", "PHONEPE"),
    ("paytm", "PAYTM"),
    ("bank transfer", "BANK_TRANSFER"),
    ("net banking", "BANK_TRANSFER"),
    ("netbanking", "BANK_TRANSFER"),
    ("upi", "UPI"),
    ("imps", "IMPS"),
    ("neft", "NEFT"),
    ("rtgs", "RTGS"),
    ("atm", "ATM"),
    ("card", "CARD"),
    ("cash", "CASH"),
]

_STOPWORDS = {
    "show", "list", "find", "display", "all", "the", "my", "me", "i", "we",
    "with", "to", "from", "in", "on", "for", "during", "of", "and", "or",
    "please", "transaction", "transactions", "txn", "entry", "entries",
    "how", "much", "many", "did", "do", "does", "was", "were", "have", "has",
    "had", "what", "which", "who", "when", "where", "total", "totals",
    "amount", "amounts", "rs", "rupees", "inr", "sent", "send", "sent?",
    "pay", "paid", "payment", "payments", "receive", "received", "receives",
    "give", "gave", "get", "got", "spent", "spend", "spending", "between",
    "this", "that", "these", "those", "a", "an", "at", "by", "per", "into",
    "towards", "about", "overall", "recent", "recently", "last", "first",
    "only", "every", "each", "till", "until", "up", "down", "out", "off",
    "between", "before", "after", "over", "under", "again", "showed",
    "possible", "make", "made", "made?", "there", "their", "them", "they",
    "been", "being", "will", "would", "should", "can", "could", "us",
    # Glue words that describe the question, never a counterparty.
    "via", "using", "use", "through", "towards", "transact", "transacted",
    "interact", "interacted", "involving", "related", "happened", "appeared",
    # Question grammar - never part of a counterparty name.
    "is", "are", "am", "it", "its", "mine", "whom", "whether", "why",
    "then", "than", "because", "across", "let",
    # Magnitude / aggregation words used by the question, not by a name.
    "most", "more", "less", "least", "biggest", "largest", "highest",
    "lowest", "maximum", "max", "minimum", "min", "top",
    "money", "cash", "rupee", "credit", "credits", "debit", "debits",
    "balance", "closing", "ending",
    # Categories and roles - these describe what, not who.
    "merchant", "merchants", "vendor", "vendors", "shop", "shops",
    "store", "stores", "payee", "payees", "recipient", "recipients",
    "person", "people", "transfer", "transferred", "transfers", "purchase",
    "purchases", "buy", "bought", "order", "orders", "subscription",
    "subscriptions", "bill", "bills", "expense", "expenses", "cost",
    "costs", "shopping",
    # Relative periods resolved into concrete dates before extraction.
    "month", "months", "year", "years", "week", "weeks", "day", "days",
    "today", "yesterday", "tonight", "current", "previous", "past",
}


@dataclass
class QuestionProfile:
    raw_question: str
    intent: str = "list"  # total_sent | total_received | count | largest | list | generic
    person_text: str | None = None
    payment_method: str | None = None
    direction: str | None = None  # DEBIT | CREDIT
    month: int | None = None
    year: int | None = None
    tokens: list[str] = field(default_factory=list)
    # Explicit "between <date> and <date>" style bounds (relative periods).
    date_from: date | None = None
    date_to: date | None = None


@dataclass
class TxnFilters:
    person_ids: list[int] | None = None
    date_from: date | None = None
    date_to: date | None = None
    month_ranges: list[tuple[date, date]] | None = None
    payment_method: str | None = None
    # Set instead of payment_method once a family (e.g. all UPI apps) matched.
    payment_methods: list[str] | None = None
    direction: str | None = None
    # Explicit combination scope. None = every statement of the user;
    # [] = nothing matched the requested scope (answer stays unanswered).
    statement_ids: list[int] | None = None


@dataclass
class PersonResolution:
    status: str = "none"  # none | resolved | ambiguous
    persons: list[Person] = field(default_factory=list)


@dataclass
class RetrievalResult:
    mode: str  # STRUCTURED_SEARCH | RAG | RAG_PLUS_STRUCTURED_SEARCH
    rows: list[Transaction] = field(default_factory=list)
    person_resolution: PersonResolution = field(default_factory=PersonResolution)
    filters: TxnFilters = field(default_factory=TxnFilters)
    notes: list[str] = field(default_factory=list)
    # True when `rows` came from complete SQL filtering (safe for exact
    # aggregates). False for vector-only samples where totals would be partial.
    structured_complete: bool = False


def understand_question(question: str) -> QuestionProfile:
    profile = QuestionProfile(raw_question=question or "")
    text = question or ""
    lowered = text.lower()

    # Payment method phrase.
    for phrase, method in _METHOD_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", lowered):
            profile.payment_method = method
            lowered = lowered.replace(phrase, " ")
            text = re.sub(rf"\b{re.escape(phrase)}\b", " ", text, flags=re.I)
            break

    # Relative period -> concrete month/year ("last month", "this year").
    relative_match = _RELATIVE_RE.search(lowered)
    if relative_match:
        span = relative_match.group(2).lower()
        back = relative_match.group(1).lower() in {"last", "previous", "past"}
        today = date.today()
        if span == "month":
            if back:
                profile.year, profile.month = (
                    (today.year - 1, 12) if today.month == 1
                    else (today.year, today.month - 1)
                )
            else:
                profile.year, profile.month = today.year, today.month
        elif span == "year":
            profile.year = today.year - 1 if back else today.year
        else:  # week -> a concrete 7 day window
            from datetime import timedelta

            profile.date_to = today
            profile.date_from = today - timedelta(days=6)
        lowered = _RELATIVE_RE.sub(" ", lowered)
        text = _RELATIVE_RE.sub(" ", text)

    # Month / year ("September 2025", bare "September", bare "2025").
    month_match = _MONTH_RE.search(lowered)
    if month_match:
        profile.month = _MONTHS[month_match.group(1).lower()]
        if month_match.group(2):
            profile.year = int(month_match.group(2))
        lowered = _MONTH_RE.sub(" ", lowered)
        text = _MONTH_RE.sub(" ", text)
    elif not profile.year:
        year_match = _YEAR_RE.search(lowered)
        if year_match:
            profile.year = int(year_match.group(0))
            lowered = _YEAR_RE.sub(" ", lowered)
            text = _YEAR_RE.sub(" ", text)

    # Intent.
    if _COUNT_RE.search(lowered):
        profile.intent = "count"
    elif _TOP_COUNTERPARTY_RE.search(lowered):
        profile.intent = "top_counterparty"
        profile.direction = "CREDIT" if _RECEIVED_RE.search(lowered) else "DEBIT"
    elif _BALANCE_RE.search(lowered):
        profile.intent = "balance"
    elif _SENT_RE.search(lowered) and _RECEIVED_RE.search(lowered):
        profile.intent = "overall_totals"
    elif _LARGEST_RE.search(lowered):
        profile.intent = "largest"
        profile.direction = "DEBIT"
    elif _SENT_RE.search(lowered):
        profile.intent = "total_sent"
        profile.direction = "DEBIT"
    elif _RECEIVED_RE.search(lowered):
        profile.intent = "total_received"
        profile.direction = "CREDIT"
    elif re.search(r"\b(who|whom)\b", lowered):
        # "Who did I pay?" -> break the counterparties down.
        profile.intent = "counterparties"
    elif _LIST_RE.search(lowered):
        profile.intent = "list"

    # Quoted person names win.
    quoted = re.search(r"[\"']([^\"']+)[\"']", text)
    if quoted:
        person_text = quoted.group(1).strip()
        text = text.replace(quoted.group(0), " ")
    else:
        words = re.findall(r"[A-Za-z][A-Za-z'.-]*", text)
        keep = [
            word.strip("'.-")
            for word in words
            if word.strip("'.-")
            and word.strip("'.-").lower() not in _STOPWORDS
            and word.strip("'.-").lower() not in _MONTHS
            and word.strip("'.-").lower() not in {p for p, _ in _METHOD_PHRASES}
        ]
        person_text = " ".join(keep).strip()

    profile.person_text = person_text.lower() or None if person_text else None
    profile.tokens = re.findall(r"[a-z0-9]+", (profile.person_text or "").lower())
    return profile


def resolve_person_candidates(db: Session, user_id: int, person_text: str) -> list[Person]:
    """Find person entities the user's own transactions point at.

    Names alone are weak ("kalyan" often appears ONLY inside a UPI handle
    such as kalyanijay@sbin), so strong identifiers are consulted too - a
    single-identifier hit answers directly, several stay ambiguous.
    """
    base = (
        db.query(Person)
        .join(Transaction, Transaction.counterparty_id == Person.id)
        .join(Statement, Statement.id == Transaction.statement_id)
        .filter(Statement.user_id == user_id)
        .distinct()
    )

    exact = base.filter(Person.normalized_name == person_text).all()
    if exact:
        return exact

    conditions = []
    for token in profile_tokens(person_text):
        if len(token) >= 3:
            conditions.append(Person.normalized_name.contains(token))
    if len(person_text) >= 3:
        conditions.append(literal(person_text).contains(Person.normalized_name))

    # UPI/account identifiers: "kalyan" matches kalyanijay@sbin even though
    # no stored person NAME contains it.
    identifier_people: list[Person] = []
    if len(person_text) >= 4:
        identifier_people = (
            db.query(Person)
            .join(PersonIdentifier, PersonIdentifier.person_id == Person.id)
            .join(Transaction, Transaction.counterparty_id == Person.id)
            .join(Statement, Statement.id == Transaction.statement_id)
            .filter(Statement.user_id == user_id)
            .filter(PersonIdentifier.id_type.in_([IdentifierType.UPI, IdentifierType.ACCOUNT]))
            .filter(PersonIdentifier.normalized_value.contains(person_text))
            .distinct()
            .limit(20)
            .all()
        )

    people = base.filter(or_(*conditions)).limit(20).all() if conditions else []
    if not conditions and not identifier_people:
        return []

    wanted = set(profile_tokens(person_text))
    overlapping = [
        person
        for person in people
        if wanted & set((person.normalized_name or "").split())
    ]
    if len(overlapping) <= 1 and not identifier_people:
        return overlapping

    # Identifier hits are authoritative: the UPI handle IS the question's
    # subject ("total sent kalyan" -> kalyanijay@sbin). Name-only people are
    # unioned in, but the leading-token preference must not drop identifier
    # matches whose stored NAME happens to lack the questioned token.
    if identifier_people:
        id_set = {person.id for person in identifier_people}
        combined = {person.id: person for person in identifier_people}
        for person in overlapping:
            if person.id not in id_set:
                combined[person.id] = person
        return sorted(combined.values(), key=lambda person: person.id)

    # Several people share *a* token. Prefer the ones matching the LEADING
    # token of the question (the given name): otherwise a generic token like
    # "kumar" drags unrelated people into an ordinary question.
    tokens = profile_tokens(person_text)
    first = tokens[0] if tokens else None
    if first:
        primary = [
            person
            for person in overlapping
            if first in (person.normalized_name or "").split()
        ]
        if primary:
            return primary
    return overlapping


def profile_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _shared_upi_local(
    db: Session, persons: list[Person], person_text: str
) -> str | None:
    """The single UPI handle local part (before @) all persons share, if any.

    Only handles containing the questioned text count, so an unrelated extra
    handle on one person cannot block the merge.
    """
    if not person_text or not persons:
        return None
    rows = (
        db.query(PersonIdentifier.normalized_value)
        .filter(
            PersonIdentifier.person_id.in_([person.id for person in persons]),
            PersonIdentifier.id_type == IdentifierType.UPI,
            PersonIdentifier.normalized_value.contains(person_text),
        )
        .all()
    )
    locals_ = {
        (value or "").partition("@")[0].strip().lower()
        for (value,) in rows
        if value and value.partition("@")[0].strip()
    }
    return locals_.pop() if len(locals_) == 1 else None


def _month_ranges(db: Session, user_id: int, month: int) -> list[tuple[date, date]]:
    """Resolve 'September' (no year) against the years present in the data."""
    years = {
        row.year
        for (row,) in db.query(Transaction.transaction_date)
        .join(Statement, Statement.id == Transaction.statement_id)
        .filter(Statement.user_id == user_id)
        .filter(Transaction.transaction_date.isnot(None))
        .limit(50000)
        .all()
        if row is not None and row.month == month
    }
    ranges: list[tuple[date, date]] = []
    import calendar

    for year in sorted(years):
        last_day = calendar.monthrange(year, month)[1]
        ranges.append((date(year, month, 1), date(year, month, last_day)))
    return ranges


def resolve_statement_scope(
    db: Session,
    user_id: int,
    statement_ids: list[int] | None,
    banks: list[str] | None,
) -> list[int] | None:
    """Turn a user-chosen scope (statement ids and/or bank names) into the
    concrete set of statement ids to answer from.

    Returns None when no scope was requested (answer from everything).
    Ids are re-checked against the owner so a foreign id can never widen the
    scope beyond this user's own statements.
    """
    if not statement_ids and not banks:
        return None

    query = db.query(Statement.id).filter(Statement.user_id == user_id)
    conditions = []
    if statement_ids:
        conditions.append(Statement.id.in_(statement_ids))
    if banks:
        cleaned = [bank.strip().lower() for bank in banks if bank and bank.strip()]
        if cleaned:
            conditions.append(func.lower(Statement.bank_name).in_(cleaned))
    if not conditions:
        return []
    # Union: "these statements" OR "these banks" - how a picker reads.
    return sorted(row[0] for row in query.filter(or_(*conditions)).all())


def build_query(db: Session, user_id: int, filters: TxnFilters):
    query = (
        db.query(Transaction)
        .join(Statement, Statement.id == Transaction.statement_id)
        .filter(Statement.user_id == user_id)
    )
    if filters.statement_ids is not None:
        query = query.filter(Transaction.statement_id.in_(filters.statement_ids))
    if filters.person_ids:
        query = query.filter(Transaction.counterparty_id.in_(filters.person_ids))
    elif filters.person_ids == []:
        query = query.filter(Transaction.counterparty_id.is_(None))

    # Explicit bounds are conjunctive: date_from <= d <= date_to.
    if filters.date_from:
        query = query.filter(Transaction.transaction_date >= filters.date_from)
    if filters.date_to:
        query = query.filter(Transaction.transaction_date <= filters.date_to)
    if filters.month_ranges:
        month_conditions = []
        for start, end in filters.month_ranges:
            month_conditions.append(
                and_(
                    Transaction.transaction_date >= start,
                    Transaction.transaction_date <= end,
                )
            )
        query = query.filter(or_(*month_conditions))

    if filters.payment_methods:
        query = query.filter(Transaction.payment_method.in_(filters.payment_methods))
    elif filters.payment_method:
        query = query.filter(Transaction.payment_method == filters.payment_method)
    if filters.direction:
        query = query.filter(Transaction.transaction_type == filters.direction)
    return query


def _structured_rows(
    db: Session,
    user_id: int,
    filters: TxnFilters,
    notes: list[str],
    extra_condition=None,
    limit: int = 500,
) -> list[Transaction]:
    """Run the structured query; widen an umbrella method only if it missed."""

    def _run() -> list[Transaction]:
        query = build_query(db, user_id, filters)
        if extra_condition is not None:
            query = query.filter(extra_condition)
        return query.order_by(
            Transaction.transaction_date.desc().nullslast(), Transaction.id.desc()
        ).limit(limit).all()

    rows = _run()
    if rows or not filters.payment_method:
        return rows

    family = _METHOD_FAMILY.get(filters.payment_method)
    if not family:
        return rows
    filters.payment_method = None
    filters.payment_methods = list(family)
    rows = _run()
    if rows:
        notes.append("METHOD_FILTER_WIDENED")
    return rows


def _matches_filters(transaction: Transaction, filters: TxnFilters) -> bool:
    """Semantic hits must still honour the question's method/direction."""
    if filters.direction and transaction.transaction_type != filters.direction:
        return False
    if filters.payment_method and transaction.payment_method != filters.payment_method:
        return False
    if (
        filters.payment_methods
        and transaction.payment_method not in filters.payment_methods
    ):
        return False
    return True


def _apply_period(filters: TxnFilters, profile: QuestionProfile, db: Session, user_id: int) -> None:
    """Turn parsed month/year/relative text into SQL date bounds."""
    if profile.month and profile.year:
        import calendar

        last_day = calendar.monthrange(profile.year, profile.month)[1]
        filters.month_ranges = [
            (date(profile.year, profile.month, 1), date(profile.year, profile.month, last_day))
        ]
    elif profile.month:
        # "September" with no year: use every year present in the data.
        filters.month_ranges = _month_ranges(db, user_id, profile.month)
    elif profile.year:
        filters.date_from = date(profile.year, 1, 1)
        filters.date_to = date(profile.year, 12, 31)
    elif profile.date_from or profile.date_to:
        filters.date_from = profile.date_from
        filters.date_to = profile.date_to


def _period_start(filters: TxnFilters) -> date | None:
    starts = [start for start, _ in filters.month_ranges or []]
    bounds = [value for value in (filters.date_from, *starts) if value]
    return min(bounds) if bounds else None


def _period_end(filters: TxnFilters) -> date | None:
    ends = [end for _, end in filters.month_ranges or []]
    bounds = [value for value in (filters.date_to, *ends) if value]
    return max(bounds) if bounds else None


def retrieve(
    db: Session,
    settings: Settings,
    user_id: int,
    profile: QuestionProfile,
    statement_scope: list[int] | None = None,
) -> RetrievalResult:
    filters = TxnFilters(
        payment_method=profile.payment_method,
        direction=profile.direction,
        statement_ids=statement_scope,
    )
    result = RetrievalResult(mode="STRUCTURED_SEARCH", filters=filters)
    _apply_period(filters, profile, db, user_id)

    # Empty scope (ids/banks requested but nothing matched): answer nothing
    # rather than silently falling back to every statement.
    if statement_scope == []:
        result.notes.append("SCOPE_EMPTY")
        result.structured_complete = True
        return result

    # --- 1) Person entity resolution (identity comes from matching, not RAG) ---
    if profile.person_text:
        persons = resolve_person_candidates(db, user_id, profile.person_text)
        if len(persons) == 1:
            result.person_resolution = PersonResolution("resolved", persons)
            filters.person_ids = [persons[0].id]
        elif len(persons) > 1:
            names = {
                (person.normalized_name or person.canonical_name or "").strip().lower()
                for person in persons
            }
            shared_local = _shared_upi_local(db, persons, profile.person_text)
            same_name = len(names) == 1 and all(names)
            if same_name or shared_local:
                # Same counterparty split across duplicate person records
                # (re-uploads, extra UPI handles): answer once, listing the
                # records, instead of refusing an ordinary question.
                result.person_resolution = PersonResolution("resolved", persons)
                filters.person_ids = [person.id for person in persons]
                result.notes.append(
                    "SAME_NAME_RECORDS_MERGED" if same_name else "SAME_UPI_RECORDS_MERGED"
                )
            else:
                result.person_resolution = PersonResolution("ambiguous", persons)
                filters.person_ids = [person.id for person in persons]
                result.notes.append("MULTIPLE_PERSON_MATCHES")
                result.structured_complete = False
                return result
        # else: fall through to description-text structured search.

    # --- 2) Structured filtering ---------------------------------------------
    if profile.person_text and filters.person_ids is None:
        # Person question but no person entity: structured search over the
        # normalized description and UPI handles - never unconstrained rows,
        # never guesses.
        rows: list[Transaction] = []
        name_tokens = [t for t in profile_tokens(profile.person_text) if len(t) >= 3]
        if name_tokens:
            rows = _structured_rows(
                db,
                user_id,
                filters,
                result.notes,
                extra_condition=or_(
                    *[
                        Transaction.normalized_counterparty_name.contains(token)
                        for token in name_tokens
                    ],
                    *[
                        Transaction.normalized_upi_id.contains(token)
                        for token in name_tokens
                    ],
                ),
            )
        if rows:
            person_ids = {
                row.counterparty_id for row in rows if row.counterparty_id
            }
            names = {
                row.normalized_counterparty_name
                for row in rows
                if row.normalized_counterparty_name
            }
            if not person_ids and len(names) > 1:
                result.rows = rows
                result.notes.append("MULTIPLE_POSSIBLE_MATCHES")
                result.person_resolution = PersonResolution("ambiguous", [])
                result.structured_complete = False
                return result
            result.notes.append("MATCHED_BY_DESCRIPTION_TEXT")
    else:
        rows = _structured_rows(db, user_id, filters, result.notes)

    result.rows = rows
    result.structured_complete = True

    if rows:
        # Structured rows are the answer set. Semantic retrieval re-ranks them
        # so the most question-relevant transactions are shown first.
        if settings.rag_enabled:
            provider = embedding_module.get_embedding_provider(settings)
            vector = provider.embed([profile.raw_question])[0]
            scored = vector_store.search(
                db,
                user_id,
                vector,
                payment_method=filters.payment_method,
                date_from=_period_start(filters),
                date_to=_period_end(filters),
                statement_ids=filters.statement_ids,
                top_k=10,
            )
            structured_ids = {row.id for row in rows}
            semantic = [
                item.transaction
                for item in scored
                if item.transaction is not None
                and item.transaction.id in structured_ids
            ]
            if semantic:
                semantic_ids = {row.id for row in semantic}
                remainder = [row for row in rows if row.id not in semantic_ids]
                result.rows = semantic + remainder
                result.mode = "RAG_PLUS_STRUCTURED_SEARCH"
                result.notes.append("SEMANTIC_RE_RANK")
        return result

    if profile.person_text:
        # A person question with no structured hit: only clearly related
        # documents may answer it - unrelated rows are never substituted.
        if settings.rag_enabled:
            provider = embedding_module.get_embedding_provider(settings)
            vector = provider.embed([profile.raw_question])[0]
            scored = vector_store.search(
                db,
                user_id,
                vector,
                payment_method=filters.payment_method,
                date_from=_period_start(filters),
                date_to=_period_end(filters),
                statement_ids=filters.statement_ids,
                top_k=5,
            )
            strong = [item for item in scored if item.score >= _SEMANTIC_MIN_SCORE]
            semantic_rows = [
                item.transaction
                for item in strong
                if item.transaction is not None
                and _matches_filters(item.transaction, filters)
            ]
            if semantic_rows:
                result.rows = semantic_rows
                result.mode = "RAG"
                result.notes.append("SEMANTIC_RETRIEVAL")
                result.structured_complete = False
        return result

    # --- 3) Semantic fallback for fuzzy questions ------------------------------
    if settings.rag_enabled:
        provider = embedding_module.get_embedding_provider(settings)
        vector = provider.embed([profile.raw_question])[0]
        scored = vector_store.search(
            db,
            user_id,
            vector,
            payment_method=filters.payment_method,
            date_from=_period_start(filters),
            date_to=_period_end(filters),
            statement_ids=filters.statement_ids,
            top_k=8,
        )
        semantic_rows = [
            item.transaction
            for item in scored
            if item.transaction is not None and _matches_filters(item.transaction, filters)
        ]
        if semantic_rows:
            result.rows = semantic_rows
            result.mode = (
                "RAG_PLUS_STRUCTURED_SEARCH"
                if (profile.payment_method or profile.direction)
                else "RAG"
            )
            result.notes.append("SEMANTIC_RETRIEVAL")
            result.structured_complete = False
    return result


def _money(value) -> Decimal | None:
    """Always two decimal places - SQL sums come back unscaled when empty."""
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"))


def compute_aggregates(
    db: Session, user_id: int, filters: TxnFilters
) -> dict:
    """Exact financial aggregates - SQL only, never LLM arithmetic."""
    query = build_query(db, user_id, filters)
    row = query.with_entities(
        func.count(Transaction.id),
        func.coalesce(func.sum(Transaction.debit_amount), 0),
        func.coalesce(func.sum(Transaction.credit_amount), 0),
        func.min(Transaction.transaction_date),
        func.max(Transaction.transaction_date),
    ).one()

    largest_row = (
        build_query(db, user_id, filters)
        .with_entities(func.max(Transaction.debit_amount))
        .one()
    )
    return {
        "count": int(row[0] or 0),
        "total_debit": _money(row[1]),
        "total_credit": _money(row[2]),
        "first_date": str(row[3]) if row[3] else None,
        "last_date": str(row[4]) if row[4] else None,
        "largest_debit": _money(largest_row[0]),
    }


def compute_counterparties(
    db: Session,
    user_id: int,
    filters: TxnFilters,
    limit: int = 10,
    rank_by: str = "amount",
) -> dict:
    """Rank counterparties inside the filtered rows - grouped by SQL, never by the model.

    rank_by="amount" orders by money moved (who did I pay the most),
    rank_by="count" orders by how often (who do I transact with most).
    """
    if filters.direction == "CREDIT":
        rank_index = 3  # total_credit
    elif filters.direction == "DEBIT":
        rank_index = 2  # total_debit
    else:
        rank_index = 4  # debit + credit

    grouped: list[dict] = []
    matched = (
        build_query(db, user_id, filters)
        .outerjoin(Person, Person.id == Transaction.counterparty_id)
        .filter(Transaction.counterparty_id.isnot(None))
        .with_entities(
            Transaction.counterparty_id,
            Person.code,
            Person.canonical_name,
            func.coalesce(func.sum(Transaction.debit_amount), 0),
            func.coalesce(func.sum(Transaction.credit_amount), 0),
            func.count(Transaction.id),
        )
        .group_by(Transaction.counterparty_id, Person.code, Person.canonical_name)
        .all()
    )
    for row in matched:
        grouped.append(
            {
                "person_id": row[0],
                "code": row[1],
                "name": row[2],
                "total_debit": _money(row[3]),
                "total_credit": _money(row[4]),
                "count": int(row[5] or 0),
            }
        )

    unmatched = (
        build_query(db, user_id, filters)
        .filter(Transaction.counterparty_id.is_(None))
        .filter(Transaction.normalized_counterparty_name.isnot(None))
        .with_entities(
            Transaction.normalized_counterparty_name,
            func.coalesce(func.sum(Transaction.debit_amount), 0),
            func.coalesce(func.sum(Transaction.credit_amount), 0),
            func.count(Transaction.id),
        )
        .group_by(Transaction.normalized_counterparty_name)
        .all()
    )
    for row in unmatched:
        grouped.append(
            {
                "person_id": None,
                "code": None,
                "name": str(row[0]).title(),
                "total_debit": _money(row[1]),
                "total_credit": _money(row[2]),
                "count": int(row[3] or 0),
            }
        )

    def _rank(entry: dict):
        if rank_by == "count":
            return (entry["count"], entry["total_debit"] or 0, entry["total_credit"] or 0)
        if rank_index == 3:
            value = entry["total_credit"]
        elif rank_index == 2:
            value = entry["total_debit"]
        else:
            value = (entry["total_debit"] or 0) + (entry["total_credit"] or 0)
        return (value or 0, entry["count"])

    grouped.sort(key=_rank, reverse=True)
    return {"distinct": len(grouped), "top": grouped[:limit]}


def compute_closing_balance(db: Session, user_id: int, filters: TxnFilters) -> dict:
    """Closing balance of the most recent matching row - read from PostgreSQL."""
    row = (
        build_query(db, user_id, filters)
        .order_by(Transaction.transaction_date.desc().nullslast(), Transaction.id.desc())
        .with_entities(Transaction.balance, Transaction.transaction_date)
        .first()
    )
    return {
        "closing_balance": _money(row[0]) if row else None,
        "balance_date": str(row[1]) if row and row[1] else None,
    }
