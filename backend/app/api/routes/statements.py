"""Statement upload/list/detail endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.api.deps import client_ip, get_current_user
from app.api.queries import apply_transaction_filters
from app.config import Settings, get_settings
from app.database.connection import get_db
from app.models import ProcessingJob, Statement, Transaction, User
from app.models.enums import StatementStatus
from app.schemas.statement import (
    BankListOut,
    BankSummaryOut,
    BatchUploadResponse,
    JobOut,
    StatementDetailOut,
    StatementListOut,
    StatementOut,
    UploadItemError,
    UploadResponse,
)
from app.schemas.transaction import TransactionListOut, TransactionOut
from app.services.processing import start_processing
from app.services.upload import (
    DuplicateUploadError,
    FileValidationError,
    create_statement,
)

router = APIRouter(prefix="/statements", tags=["statements"])

# One batch keeps an import session bounded (and one bad file can't exhaust RAM).
_MAX_BATCH_FILES = 10


@router.post("/upload", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_statement(
    request: Request,
    file: UploadFile = File(...),
    name: str | None = Form(default=None),
    password: str | None = Form(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Upload a statement (PDF/CSV/XLS/XLSX).

    `password` is used only for this extraction run - it is never stored,
    never logged and never returned.
    """
    if not file.filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No file provided.")

    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"File exceeds the {settings.max_upload_size_mb} MB upload limit.",
        )
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The uploaded file is empty.")

    try:
        statement, job = create_statement(
            db,
            settings,
            user,
            filename=file.filename,
            data=data,
            content_type=file.content_type,
            display_name=name,
            ip_address=client_ip(request),
        )
    except DuplicateUploadError as error:
        # 409: the identical file is already parsed - never double-count.
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    except FileValidationError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error

    # The password stays in this call frame only (thread-local memory).
    start_processing(statement.id, password)

    return UploadResponse(
        statement=StatementOut.model_validate(statement),
        job=JobOut.model_validate(job),
    )


@router.post(
    "/upload-batch",
    response_model=BatchUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_statements_batch(
    request: Request,
    files: list[UploadFile] = File(...),
    password: str | None = Form(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Upload several statements in one request.

    Partial success is allowed: each file is validated independently, so a
    duplicate or a corrupt file is reported per file while the rest keep
    processing. `password` (if given) is applied to every file in the batch
    and, as with single upload, lives only in this call frame.
    """
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No files provided.")
    if len(files) > _MAX_BATCH_FILES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"A batch may contain at most {_MAX_BATCH_FILES} files.",
        )

    uploaded: list[UploadResponse] = []
    errors: list[UploadItemError] = []
    seen_names: set[str] = set()

    for file in files:
        filename = file.filename or ""
        if not filename:
            errors.append(UploadItemError(filename="(unnamed)", error="No file provided."))
            continue

        # Same name twice inside ONE batch would race the duplicate check.
        if filename in seen_names:
            errors.append(
                UploadItemError(
                    filename=filename,
                    error="Duplicate filename inside this batch - rename one of the files.",
                )
            )
            continue
        seen_names.add(filename)

        data = await file.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            errors.append(
                UploadItemError(
                    filename=filename,
                    error=f"File exceeds the {settings.max_upload_size_mb} MB upload limit.",
                )
            )
            continue
        if not data:
            errors.append(UploadItemError(filename=filename, error="The uploaded file is empty."))
            continue

        try:
            statement, job = create_statement(
                db,
                settings,
                user,
                filename=filename,
                data=data,
                content_type=file.content_type,
                ip_address=client_ip(request),
            )
        except DuplicateUploadError as error:
            errors.append(UploadItemError(filename=filename, error=str(error)))
            continue
        except FileValidationError as error:
            errors.append(UploadItemError(filename=filename, error=str(error)))
            continue

        start_processing(statement.id, password)
        uploaded.append(
            UploadResponse(
                statement=StatementOut.model_validate(statement),
                job=JobOut.model_validate(job),
            )
        )

    return BatchUploadResponse(uploaded=uploaded, errors=errors)


@router.get("", response_model=StatementListOut)
def list_statements(
    status_filter: str | None = Query(default=None, alias="status"),
    bank: str | None = Query(
        default=None, max_length=64,
        description="Exact bank name (case-insensitive), e.g. 'HDFC Bank'.",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Statement).filter(Statement.user_id == user.id)
    if status_filter:
        query = query.filter(Statement.status == status_filter)
    if bank:
        query = query.filter(func.lower(Statement.bank_name) == bank.strip().lower())
    total = query.count()
    items = (
        query.order_by(Statement.id.desc()).offset(offset).limit(limit).all()
    )
    return StatementListOut(
        total=total,
        items=[StatementOut.model_validate(item) for item in items],
    )


@router.get("/banks", response_model=BankListOut)
def list_banks(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Distinct banks across this user's statements (for filter pickers)."""
    rows = (
        db.query(
            Statement.bank_name,
            func.count(Statement.id),
            func.coalesce(func.sum(Statement.transaction_count), 0),
        )
        .filter(Statement.user_id == user.id, Statement.bank_name.isnot(None))
        .group_by(Statement.bank_name)
        .order_by(func.count(Statement.id).desc(), Statement.bank_name.asc())
        .all()
    )
    items = [
        BankSummaryOut(
            bank_name=row[0],
            statement_count=int(row[1] or 0),
            transaction_count=int(row[2] or 0),
        )
        for row in rows
        if row[0]
    ]
    return BankListOut(total=len(items), items=items)


def _latest_job(db: Session, statement_id: int) -> ProcessingJob | None:
    return (
        db.query(ProcessingJob)
        .filter(ProcessingJob.statement_id == statement_id)
        .order_by(ProcessingJob.id.desc())
        .first()
    )


@router.get("/{statement_id}", response_model=StatementDetailOut)
def get_statement(
    statement_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = (
        db.query(Statement)
        .filter(Statement.id == statement_id, Statement.user_id == user.id)
        .first()
    )
    if statement is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Statement not found")

    job = _latest_job(db, statement_id)
    warnings: list[str] = []
    if (
        job is not None
        and job.status == "COMPLETED"
        and job.message
        and job.message != "Processing complete"
    ):
        warnings = [part.strip() for part in job.message.split(";") if part.strip()]

    payload = StatementDetailOut.model_validate(statement)
    payload.latest_job = JobOut.model_validate(job) if job else None
    payload.warnings = warnings
    return payload


@router.get("/{statement_id}/transactions", response_model=TransactionListOut)
def list_statement_transactions(
    statement_id: int,
    q: str | None = Query(default=None, max_length=200),
    person: str | None = Query(default=None, max_length=64),
    method: str | None = Query(default=None, max_length=32),
    match_status: str | None = Query(default=None, max_length=16),
    txn_type: str | None = Query(default=None, alias="type", max_length=16),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = (
        db.query(Statement)
        .filter(Statement.id == statement_id, Statement.user_id == user.id)
        .first()
    )
    if statement is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Statement not found")

    query = db.query(Transaction).filter(Transaction.statement_id == statement_id)
    query = apply_transaction_filters(
        query, q, person, method, match_status, txn_type, date_from, date_to, db, user
    )
    total = query.count()
    items = (
        query.options(joinedload(Transaction.person))
        .order_by(
            Transaction.transaction_date.desc().nullslast(), Transaction.id.desc()
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    return TransactionListOut(
        total=total,
        offset=offset,
        limit=limit,
        items=[TransactionOut.model_validate(item) for item in items],
    )
