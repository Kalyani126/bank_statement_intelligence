import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database.connection import init_db, test_database_connection
from app.security.files import FileValidationError
from app.security.masking import redact_sensitive

logging.basicConfig(
    level=get_settings().log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables + seed the admin user on boot (idempotent).
    init_db()
    yield


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(FileValidationError)
async def file_validation_handler(request: Request, error: FileValidationError):
    return JSONResponse(status_code=400, content={"detail": str(error)})


@app.get("/")
def root():
    return {
        "message": "Bank Statement Intelligence API is running"
    }


@app.get("/health")
def health():
    try:
        test_database_connection()

        return {
            "status": "healthy",
            "database": "connected",
        }

    except Exception as error:
        # Never leak connection strings/details to clients.
        logger.warning("Health check failed: %s", type(error).__name__)
        return {
            "status": "unhealthy",
            "database": "disconnected",
            "error": redact_sensitive(str(error))[:300],
        }


from app.api.routes import (  # noqa: E402 - routers import after app exists
    auth,
    dashboard,
    matches,
    people,
    processing,
    rag,
    statements,
    system,
    transactions,
)

for router in (
    auth.router,
    statements.router,
    transactions.router,
    people.router,
    matches.router,
    rag.router,
    processing.router,
    dashboard.router,
    system.router,
):
    app.include_router(router, prefix="/api/v1")
