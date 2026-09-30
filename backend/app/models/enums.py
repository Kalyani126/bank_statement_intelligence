"""Shared enumerations.

Stored as plain strings in the database so PostgreSQL and the SQLite test
fallback behave identically; the Python enums give the API type safety.
"""

from __future__ import annotations

from enum import StrEnum


class MatchingStatus(StrEnum):
    """Outcome of the person/counterparty matching decision."""

    MATCH = "MATCH"
    AMBIGUOUS = "AMBIGUOUS"
    UNKNOWN = "UNKNOWN"


class MatchingMethod(StrEnum):
    """Which layer produced the decision.

    JEV          - real TypeSafe/Jev structured decision (only when enabled
                   and the real API answered).
    LOCAL_MATCHING - deterministic local candidate matching (JEV disabled or
                   unavailable). Never reported as JEV.
    MANUAL       - human review decision.
    """

    JEV = "JEV"
    LOCAL_MATCHING = "LOCAL_MATCHING"
    MANUAL = "MANUAL"


class PaymentMethod(StrEnum):
    UPI = "UPI"
    GOOGLE_PAY = "GOOGLE_PAY"
    PHONEPE = "PHONEPE"
    PAYTM = "PAYTM"
    IMPS = "IMPS"
    NEFT = "NEFT"
    RTGS = "RTGS"
    BANK_TRANSFER = "BANK_TRANSFER"
    ATM = "ATM"
    CARD = "CARD"
    CASH = "CASH"
    OTHER = "OTHER"


class TransactionType(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"
    UNKNOWN = "UNKNOWN"


class StatementStatus(StrEnum):
    UPLOADED = "UPLOADED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class IdentifierType(StrEnum):
    UPI = "UPI"
    ACCOUNT = "ACCOUNT"
    PHONE = "PHONE"
    EMAIL = "EMAIL"
    NAME = "NAME"
    REFERENCE = "REFERENCE"


class DocumentType(StrEnum):
    TRANSACTION = "TRANSACTION"
    PERSON = "PERSON"


class AnswerSource(StrEnum):
    """How an assistant answer was produced."""

    STRUCTURED_SEARCH = "STRUCTURED_SEARCH"
    RAG = "RAG"
    RAG_PLUS_STRUCTURED_SEARCH = "RAG_PLUS_STRUCTURED_SEARCH"


class ExplanationSource(StrEnum):
    """Who phrased the answer. Financial numbers always come from SQL/Python."""

    TEMPLATE = "TEMPLATE"
    LLM = "LLM"


class PersonSource(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class AuditAction(StrEnum):
    LOGIN = "LOGIN"
    LOGIN_FAILED = "LOGIN_FAILED"
    STATEMENT_UPLOAD = "STATEMENT_UPLOAD"
    STATEMENT_PROCESSED = "STATEMENT_PROCESSED"
    MATCH_DECISION = "MATCH_DECISION"
    MANUAL_MATCH_CONFIRM = "MANUAL_MATCH_CONFIRM"
    MANUAL_MATCH_REJECT = "MANUAL_MATCH_REJECT"
    RAG_QUERY = "RAG_QUERY"
    PERSON_CREATE = "PERSON_CREATE"
    PERSON_UPDATE = "PERSON_UPDATE"
