"""ORM model registry - importing this module registers every table."""

from app.models.audit import AuditLog
from app.models.base import Base, TimestampMixin
from app.models.enums import (
    AnswerSource,
    AuditAction,
    DocumentType,
    ExplanationSource,
    IdentifierType,
    JobStatus,
    MatchingMethod,
    MatchingStatus,
    PaymentMethod,
    PersonSource,
    StatementStatus,
    TransactionType,
)
from app.models.job import ProcessingJob
from app.models.match import TransactionPersonMatch
from app.models.person import Person, PersonIdentifier
from app.models.rag import RagDocument
from app.models.statement import Statement, StatementFile
from app.models.transaction import Transaction
from app.models.user import User

__all__ = [
    "AnswerSource",
    "AuditAction",
    "AuditLog",
    "Base",
    "DocumentType",
    "ExplanationSource",
    "IdentifierType",
    "JobStatus",
    "MatchingMethod",
    "MatchingStatus",
    "PaymentMethod",
    "Person",
    "PersonIdentifier",
    "PersonSource",
    "ProcessingJob",
    "RagDocument",
    "Statement",
    "StatementFile",
    "StatementStatus",
    "TimestampMixin",
    "Transaction",
    "TransactionPersonMatch",
    "TransactionType",
    "User",
]
