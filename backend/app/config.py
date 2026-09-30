"""Central application configuration.

All secrets live here, backend-only. Nothing defined in this module is ever
exposed to the frontend: the Next.js client only talks to the HTTP API.
"""

from __future__ import annotations

import logging
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Used when SECRET_KEY is not configured. Tokens then die on restart, which is
# acceptable for development but should be set explicitly in production.
_EPHEMERAL_SECRET = secrets.token_urlsafe(48)

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Core -----------------------------------------------------------------
    app_name: str = "Bank Statement Intelligence API"
    environment: str = "development"
    database_url: str = ""
    secret_key: str = ""
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    log_level: str = "INFO"

    # --- Auth -----------------------------------------------------------------
    auth_enabled: bool = True
    admin_username: str = "admin"
    admin_password: str = "admin123"
    access_token_expire_minutes: int = 720

    # --- JEV / TypeSafe AI ----------------------------------------------------
    # JEV is the structured decision layer for identity matching. It is only
    # ever reported as "used" when these settings are configured and the real
    # TypeSafe endpoint answers.
    jev_enabled: bool = False
    typesafe_api_key: str = ""
    typesafe_api_base: str = "https://api.typesafe.ai"
    jev_model: str = "jev-latest"
    jev_timeout_seconds: float = 15.0

    # --- RAG ------------------------------------------------------------------
    rag_enabled: bool = True
    embedding_provider: str = "local"  # local | openai
    embedding_dimensions: int = 384
    llm_provider: str = "none"  # none | openai
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"

    # --- Security / uploads ---------------------------------------------------
    max_upload_size_mb: int = 20
    upload_dir: str = str(BACKEND_DIR / "storage" / "uploads")
    allowed_extensions: str = ".pdf,.csv,.xls,.xlsx"

    # --- Derived helpers ------------------------------------------------------
    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def allowed_extension_list(self) -> list[str]:
        return [ext.strip().lower() for ext in self.allowed_extensions.split(",") if ext.strip()]

    @property
    def secret(self) -> str:
        return self.secret_key or _EPHEMERAL_SECRET

    @property
    def jev_configured(self) -> bool:
        """True only when JEV is switched on AND real credentials exist."""
        return self.jev_enabled and bool(self.typesafe_api_key)

    @property
    def llm_configured(self) -> bool:
        return self.llm_provider.lower() == "openai" and bool(self.openai_api_key)

    @property
    def embeddings_configured(self) -> bool:
        if self.embedding_provider.lower() == "openai":
            return bool(self.openai_api_key)
        return True  # deterministic local embedding always available


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    if not settings.secret_key:
        logger.warning(
            "SECRET_KEY is not set - using an ephemeral key; auth tokens will "
            "invalidate when the server restarts."
        )
    if settings.jev_enabled and not settings.typesafe_api_key:
        logger.error(
            "JEV_ENABLED=true but TYPESAFE_API_KEY is empty. JEV will NOT be "
            "called; matching falls back to LOCAL_MATCHING with manual review."
        )
    return settings
