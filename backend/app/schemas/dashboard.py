"""Dashboard and system status schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DashboardSummary(BaseModel):
    totals: dict[str, Any]
    statements_by_status: dict[str, int] = Field(default_factory=dict)
    charts: dict[str, Any] = Field(default_factory=dict)


class SystemStatusOut(BaseModel):
    """Backend capability status. NEVER includes any secret."""

    app_name: str
    environment: str
    jev_enabled: bool
    jev_configured: bool
    jev_model: str
    typesafe_api_base: str
    rag_enabled: bool
    embedding_provider: str
    llm_provider: str
    llm_configured: bool
    auth_enabled: bool
    max_upload_size_mb: int
    allowed_extensions: list[str]


class ReindexOut(BaseModel):
    """Result of a RAG re-index for the calling account."""

    documents: int
    statements: int
    embedding_provider: str


class AuditLogOut(BaseModel):
    id: int
    action: str
    entity_type: str | None = None
    entity_id: str | None = None
    details: dict[str, Any] | None = None
    created_at: str | None = None

    model_config = {"from_attributes": True}
