"""AI/RAG assistant endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import client_ip, get_current_user
from app.config import Settings, get_settings
from app.database.connection import get_db
from app.models import User
from app.rag.service import query as rag_query
from app.schemas.rag import RAGQueryRequest, RAGQueryResponse

router = APIRouter(prefix="/rag", tags=["rag"])


@router.post("/query", response_model=RAGQueryResponse)
def query(
    payload: RAGQueryRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    return rag_query(
        db,
        settings,
        user,
        payload.question,
        ip_address=client_ip(request),
        statement_ids=payload.statement_ids,
        banks=payload.banks,
    )
