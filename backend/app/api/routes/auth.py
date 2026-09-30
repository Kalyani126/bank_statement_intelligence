"""Auth endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import client_ip, get_current_user
from app.config import Settings, get_settings
from app.database.connection import get_db
from app.models import AuditAction, User
from app.schemas.auth import LoginRequest, TokenResponse, UserOut
from app.security.audit import write_audit
from app.security.auth import create_access_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    user = db.query(User).filter(User.username == payload.username).first()
    authenticated = (
        user is not None
        and user.is_active
        and verify_password(payload.password, user.password_hash)
    )
    if not authenticated:
        write_audit(
            db,
            AuditAction.LOGIN_FAILED,
            user_id=user.id if user else None,
            details={"username": payload.username[:64]},
            ip_address=client_ip(request),
        )
        db.commit()
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Invalid username or password"
        )

    token = create_access_token(user.username, settings)
    write_audit(
        db,
        AuditAction.LOGIN,
        user_id=user.id,
        ip_address=client_ip(request),
    )
    db.commit()
    return TokenResponse(
        access_token=token,
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user
