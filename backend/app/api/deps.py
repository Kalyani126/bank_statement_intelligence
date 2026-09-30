"""Shared API dependencies: authentication and request metadata."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database.connection import get_db
from app.models import User
from app.security.auth import decode_access_token

_bearer = HTTPBearer(auto_error=False)

UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    if not settings.auth_enabled:
        user = db.query(User).order_by(User.id).first()
        if user is None:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "No user exists")
        return user

    if credentials is None or not credentials.credentials:
        raise UNAUTHORIZED
    username = decode_access_token(credentials.credentials, settings)
    if not username:
        raise UNAUTHORIZED
    user = db.query(User).filter(User.username == username).first()
    if user is None or not user.is_active:
        raise UNAUTHORIZED
    request.state.user_id = user.id
    return user


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    if request.client:
        return request.client.host[:64]
    return None
