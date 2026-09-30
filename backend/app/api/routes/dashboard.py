"""Dashboard endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.database.connection import get_db
from app.models import User
from app.schemas.dashboard import DashboardSummary
from app.services.dashboard import summary as dashboard_summary

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummary)
def summary(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return dashboard_summary(db, user)
