"""Upload service: validate, store securely, queue processing."""

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditAction, ProcessingJob, Statement, StatementFile, User
from app.models.enums import JobStatus, StatementStatus
from app.security.audit import write_audit
from app.security.files import (
    DuplicateUploadError,
    FileValidationError,
    save_upload,
    validate_upload,
)


def _find_duplicate(db: Session, user_id: int, sha256: str) -> Statement | None:
    """Existing statement holding the identical file (same bytes)."""
    return (
        db.query(Statement)
        .join(StatementFile, StatementFile.statement_id == Statement.id)
        .filter(Statement.user_id == user_id)
        .filter(StatementFile.sha256 == sha256)
        .filter(
            Statement.status.in_(
                [
                    StatementStatus.UPLOADED,
                    StatementStatus.PROCESSING,
                    StatementStatus.COMPLETED,
                ]
            )
        )
        .order_by(Statement.id.desc())
        .first()
    )


def create_statement(
    db: Session,
    settings: Settings,
    user: User,
    *,
    filename: str,
    data: bytes,
    content_type: str | None = None,
    display_name: str | None = None,
    ip_address: str | None = None,
) -> tuple[Statement, ProcessingJob]:
    """Validate + persist one uploaded statement and queue its job."""
    head = data[:16]
    kind = validate_upload(filename, len(data), head, settings)

    # Same bytes already on the account? Refuse instead of double-counting
    # every total and creating duplicate counterparties.
    digest = hashlib.sha256(data).hexdigest()
    existing = _find_duplicate(db, user.id, digest)
    if existing is not None:
        raise DuplicateUploadError(
            f"This exact file is already uploaded as statement #{existing.id} "
            f"\"{existing.name}\". Open that statement, or upload a different file.",
            statement_id=existing.id,
        )

    stored = save_upload(settings.upload_dir, filename, [data])

    name = (display_name or "").strip() or Path(stored.original_filename).stem

    statement = Statement(
        user_id=user.id,
        name=name[:250],
        source_file_name=stored.original_filename,
        file_type=kind.upper(),
        status=StatementStatus.UPLOADED,
    )
    db.add(statement)
    db.flush()

    db.add(
        StatementFile(
            statement_id=statement.id,
            original_filename=stored.original_filename,
            stored_path=str(stored.path),
            file_size=stored.size,
            content_type=content_type,
            sha256=stored.sha256,
            is_encrypted=False,
        )
    )
    job = ProcessingJob(
        statement_id=statement.id,
        user_id=user.id,
        job_type="STATEMENT_PROCESSING",
        status=JobStatus.QUEUED,
        stage="QUEUED",
        progress=0,
        message="Waiting to start",
    )
    db.add(job)

    write_audit(
        db,
        AuditAction.STATEMENT_UPLOAD,
        user_id=user.id,
        entity_type="statement",
        entity_id=statement.id,
        details={
            "filename": stored.original_filename,
            "size": stored.size,
            "sha256": stored.sha256,
            "kind": kind,
        },
        ip_address=ip_address,
    )
    db.commit()
    db.refresh(statement)
    db.refresh(job)
    return statement, job


__all__ = ["DuplicateUploadError", "FileValidationError", "create_statement"]
