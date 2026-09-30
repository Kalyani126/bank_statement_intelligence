"""Processing job status endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database.connection import get_db
from app.models import ProcessingJob, Statement, User
from app.schemas.statement import JobOut

router = APIRouter(prefix="/processing", tags=["processing"])


@router.get("/{job_id}", response_model=JobOut)
def get_job(
    job_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = (
        db.query(ProcessingJob)
        .join(Statement, Statement.id == ProcessingJob.statement_id)
        .filter(ProcessingJob.id == job_id, Statement.user_id == user.id)
        .first()
    )
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job
