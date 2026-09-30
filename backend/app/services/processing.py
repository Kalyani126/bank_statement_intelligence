"""Background statement processing pipeline.

Upload -> validate -> extract -> normalize -> store -> candidate matching
(JEV if enabled, else labelled LOCAL_MATCHING) -> RAG indexing.

The PDF password only ever lives in the local variable of one run: it is
never persisted, never logged, and never returned.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.database.connection import SessionLocal
from app.matching.service import match_transaction
from app.models import (
    AuditAction,
    ProcessingJob,
    RagDocument,
    Statement,
    StatementFile,
    Transaction,
    User,
)
from app.models.enums import JobStatus, PaymentMethod, StatementStatus, TransactionType
from app.parsers import EncryptedPdfError, ParserError, parse_statement
from app.rag.documents import build_transaction_document
from app.rag.embeddings import get_embedding_provider
from app.rag.vector_store import upsert_documents
from app.security.audit import write_audit
from app.security.files import FileValidationError
from app.security.masking import redact_sensitive
from app.services.normalization import normalize_description

logger = logging.getLogger(__name__)

_EMBEDDING_BATCH = 64


def start_processing(statement_id: int, password: str | None = None) -> None:
    """Kick off processing in a daemon thread (password kept in memory only)."""
    thread = threading.Thread(
        target=_run,
        args=(statement_id, password),
        daemon=True,
        name=f"statement-{statement_id}",
    )
    thread.start()


def _set_job(db: Session, job: ProcessingJob, **fields) -> None:
    for key, value in fields.items():
        setattr(job, key, value)
    db.commit()


def _fail(db: Session, statement: Statement, job: ProcessingJob, message: str) -> None:
    safe = redact_sensitive(message)[:900]
    statement.status = StatementStatus.FAILED
    statement.error_message = safe
    _set_job(
        db,
        job,
        status=JobStatus.FAILED,
        message=safe,
        error=safe,
        finished_at=datetime.now(timezone.utc),
    )


def _run(statement_id: int, password: str | None) -> None:
    settings = get_settings()
    db: Session = SessionLocal()
    try:
        statement = db.get(Statement, statement_id)
        if statement is None:
            return
        job = (
            db.query(ProcessingJob)
            .filter(ProcessingJob.statement_id == statement_id)
            .order_by(ProcessingJob.id.desc())
            .first()
        )
        if job is None:
            return

        statement.status = StatementStatus.PROCESSING
        _set_job(
            db,
            job,
            status=JobStatus.RUNNING,
            stage="EXTRACT",
            progress=5,
            message="Reading statement file",
            started_at=datetime.now(timezone.utc),
            error=None,
        )

        file_row = (
            db.query(StatementFile)
            .filter(StatementFile.statement_id == statement_id)
            .order_by(StatementFile.id)
            .first()
        )
        if file_row is None:
            _fail(db, statement, job, "Uploaded file record is missing.")
            return

        with open(file_row.stored_path, "rb") as handle:
            data = handle.read()
        if password:
            file_row.is_encrypted = True  # metadata only - never the password

        # --- 1) Extract --------------------------------------------------------
        parsed = parse_statement(
            statement.file_type.lower(),
            data,
            filename=statement.source_file_name or "",
            password=password,
        )
        if statement.bank_name is None:
            statement.bank_name = parsed.bank_name
        if statement.account_holder_name is None:
            statement.account_holder_name = parsed.account_holder
        statement.account_number_masked = parsed.account_number_raw
        statement.period_start = parsed.period_start
        statement.period_end = parsed.period_end

        _set_job(
            db, job, stage="NORMALIZE", progress=25,
            message=f"Extracted {len(parsed.rows)} rows",
        )

        # --- 2) Normalize + store ---------------------------------------------
        existing = (
            db.query(Transaction).filter(Transaction.statement_id == statement_id).count()
        )
        if existing:
            db.query(Transaction).filter(Transaction.statement_id == statement_id).delete(
                synchronize_session=False
            )
            db.flush()

        transactions: list[Transaction] = []
        for index, row in enumerate(parsed.rows):
            norm = normalize_description(row.description or row.raw_text)
            if row.debit is not None:
                transaction_type = TransactionType.DEBIT
            elif row.credit is not None:
                transaction_type = TransactionType.CREDIT
            else:
                transaction_type = TransactionType.UNKNOWN

            transaction = Transaction(
                statement_id=statement_id,
                transaction_date=row.transaction_date,
                value_date=row.value_date,
                description=row.description,
                raw_description=row.description or row.raw_text,
                narration=row.description,
                raw_text=row.raw_text,
                source_page=row.source_page,
                reference_number=row.reference or norm.reference,
                debit_amount=row.debit,
                credit_amount=row.credit,
                balance=row.balance,
                currency="INR",
                transaction_type=transaction_type,
                payment_method=(norm.payment_method or PaymentMethod.OTHER).value,
                upi_id=norm.upi_id,
                normalized_upi_id=norm.normalized_upi_id,
                counterparty_name=norm.name,
                normalized_counterparty_name=norm.normalized_name,
            )
            db.add(transaction)
            transactions.append(transaction)
            if index % 50 == 49:
                db.flush()

        db.flush()
        statement.transaction_count = len(transactions)
        db.commit()

        _set_job(
            db, job, stage="MATCH", progress=45,
            message=f"Matching counterparties for {len(transactions)} transactions",
        )

        # --- 3) Candidate matching (JEV or labelled LOCAL_MATCHING) ------------
        matched = 0
        for transaction in transactions:
            norm = normalize_description(
                transaction.raw_description or transaction.description or ""
            )
            match_transaction(db, settings, transaction, norm)
            matched += 1
            if matched % 25 == 0:
                _set_job(
                    db, job, progress=45 + int(35 * matched / max(len(transactions), 1)),
                    stage="MATCH", message=f"Matched {matched}/{len(transactions)}",
                )

        _set_job(db, job, stage="INDEX", progress=85, message="Indexing for RAG")

        # --- 4) RAG indexing -----------------------------------------------------
        if settings.rag_enabled:
            _index_rag(db, settings, statement_id)
        else:
            db.query(RagDocument).filter(
                RagDocument.statement_id == statement_id
            ).delete(synchronize_session=False)

        # --- 5) Done -------------------------------------------------------------
        statement.status = StatementStatus.COMPLETED
        statement.processed_at = datetime.now(timezone.utc)
        statement.transaction_count = len(transactions)
        write_audit(
            db,
            AuditAction.STATEMENT_PROCESSED,
            user_id=statement.user_id,
            entity_type="statement",
            entity_id=statement.id,
            details={"transactions": len(transactions)},
        )
        warnings = "; ".join(parsed.warnings[:5]) if parsed.warnings else None
        _set_job(
            db,
            job,
            status=JobStatus.COMPLETED,
            stage="DONE",
            progress=100,
            message=warnings or "Processing complete",
            finished_at=datetime.now(timezone.utc),
            error=None,
        )
    except EncryptedPdfError as error:
        db.rollback()
        _fail_job(db, statement_id, str(error), password_required=True)
    except (ParserError, FileValidationError) as error:
        db.rollback()
        _fail_job(db, statement_id, str(error))
    except Exception:  # noqa: BLE001 - never leak internals to the client
        logger.exception("Statement %s processing failed", statement_id)
        db.rollback()
        _fail_job(
            db,
            statement_id,
            "Processing failed due to an internal error. Check the server logs.",
        )
    finally:
        db.close()


def _fail_job(
    db: Session, statement_id: int, message: str, password_required: bool = False
) -> None:
    statement = db.get(Statement, statement_id)
    job = (
        db.query(ProcessingJob)
        .filter(ProcessingJob.statement_id == statement_id)
        .order_by(ProcessingJob.id.desc())
        .first()
    )
    if statement is None or job is None:
        return
    safe = redact_sensitive(message)[:900]
    if password_required:
        safe = (
            "This PDF is password-protected. Provide the password and retry."
        )
    _fail(db, statement, job, safe)


def _index_rag(db: Session, settings, statement_id: int) -> None:
    provider = get_embedding_provider(settings)
    transactions = (
        db.query(Transaction)
        .filter(Transaction.statement_id == statement_id)
        .order_by(Transaction.id)
        .all()
    )
    documents = [build_transaction_document(transaction) for transaction in transactions]

    for start in range(0, len(documents), _EMBEDDING_BATCH):
        batch = documents[start : start + _EMBEDDING_BATCH]
        vectors = provider.embed([document.content for document in batch])
        for document, vector in zip(batch, vectors):
            document.embedding = vector
            document.embedding_model = provider.name

    upsert_documents(db, documents)
    db.commit()


def reindex_user(db: Session, settings, user: User) -> int:
    """Rebuild RAG documents for every completed statement of a user."""
    provider = get_embedding_provider(settings)
    statements = (
        db.query(Statement)
        .filter(Statement.user_id == user.id)
        .filter(Statement.status == StatementStatus.COMPLETED)
        .all()
    )
    total = 0
    for statement in statements:
        transactions = (
            db.query(Transaction)
            .filter(Transaction.statement_id == statement.id)
            .all()
        )
        documents = [
            build_transaction_document(transaction) for transaction in transactions
        ]
        for start in range(0, len(documents), _EMBEDDING_BATCH):
            batch = documents[start : start + _EMBEDDING_BATCH]
            vectors = provider.embed([document.content for document in batch])
            for document, vector in zip(batch, vectors):
                document.embedding = vector
                document.embedding_model = provider.name
        total += upsert_documents(db, documents)
    db.commit()
    return total
