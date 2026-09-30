"""System status (no secrets) and audit log endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.config import Settings, get_settings
from app.database.connection import get_db
from app.models import AuditLog, Statement, User
from app.models.enums import StatementStatus
from app.schemas.dashboard import AuditLogOut, ReindexOut, SystemStatusOut
from app.services.processing import reindex_user

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/status", response_model=SystemStatusOut)
def status(settings: Settings = Depends(get_settings)):
    """Capability flags for the Settings page. Never exposes any key."""
    return SystemStatusOut(
        app_name=settings.app_name,
        environment=settings.environment,
        jev_enabled=settings.jev_enabled,
        jev_configured=settings.jev_configured,
        jev_model=settings.jev_model,
        typesafe_api_base=settings.typesafe_api_base,
        rag_enabled=settings.rag_enabled,
        embedding_provider=settings.embedding_provider,
        llm_provider=settings.llm_provider if settings.llm_configured else "none",
        llm_configured=settings.llm_configured,
        auth_enabled=settings.auth_enabled,
        max_upload_size_mb=settings.max_upload_size_mb,
        allowed_extensions=settings.allowed_extension_list,
    )


@router.post("/reindex", response_model=ReindexOut)
def reindex(
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Rebuild the RAG vector index for this account's completed statements.

    Useful after changing the embedding provider or repairing data; safe to
    run repeatedly (documents are upserted, never duplicated).
    """
    documents = reindex_user(db, settings, user)
    statements = (
        db.query(Statement)
        .filter(Statement.user_id == user.id)
        .filter(Statement.status == StatementStatus.COMPLETED)
        .count()
    )
    return ReindexOut(
        documents=documents,
        statements=statements,
        embedding_provider=settings.embedding_provider,
    )


@router.get("/audit-logs", response_model=list[AuditLogOut])
def audit_logs(
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    entries = (
        db.query(AuditLog)
        .filter(AuditLog.user_id == user.id)
        .order_by(AuditLog.id.desc())
        .limit(limit)
        .all()
    )
    return [
        AuditLogOut(
            id=entry.id,
            action=entry.action,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            details=entry.details,
            created_at=entry.created_at.isoformat() if entry.created_at else None,
        )
        for entry in entries
    ]
