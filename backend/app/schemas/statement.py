"""Statement, file and job schemas."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class JobOut(BaseModel):
    id: int
    statement_id: int
    status: str
    stage: str | None = None
    progress: int = 0
    message: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    model_config = {"from_attributes": True}


class StatementOut(BaseModel):
    id: int
    name: str
    bank_name: str | None = None
    account_holder_name: str | None = None
    account_number_masked: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    source_file_name: str | None = None
    file_type: str | None = None
    status: str
    transaction_count: int = 0
    error_message: str | None = None
    processed_at: datetime | None = None
    uploaded_at: datetime

    model_config = {"from_attributes": True}


class StatementDetailOut(StatementOut):
    latest_job: JobOut | None = None
    warnings: list[str] = Field(default_factory=list)


class UploadResponse(BaseModel):
    statement: StatementOut
    job: JobOut


class UploadItemError(BaseModel):
    """One file of a batch that could not be accepted."""

    filename: str
    error: str


class BatchUploadResponse(BaseModel):
    """Per-file outcome of a multi-file upload (partial success allowed)."""

    uploaded: list[UploadResponse] = Field(default_factory=list)
    errors: list[UploadItemError] = Field(default_factory=list)


class BankSummaryOut(BaseModel):
    bank_name: str
    statement_count: int = 0
    transaction_count: int = 0


class BankListOut(BaseModel):
    total: int
    items: list[BankSummaryOut]


class StatementListOut(BaseModel):
    total: int
    items: list[StatementOut]
