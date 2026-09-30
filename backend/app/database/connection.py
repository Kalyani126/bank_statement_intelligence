import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

DATABASE_URL = settings.database_url

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured in .env")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


def test_database_connection():
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))

    return True


def get_db():
    """FastAPI dependency yielding a scoped database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables and seed the default admin user (idempotent)."""
    from app.models import Base
    from app.models.user import User
    from app.security.auth import hash_password

    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        existing = db.query(User).filter(User.username == settings.admin_username).first()
        if existing is None:
            db.add(
                User(
                    username=settings.admin_username,
                    full_name="Administrator",
                    password_hash=hash_password(settings.admin_password),
                    role="admin",
                )
            )
            db.commit()
            logger.info("Seeded default admin user '%s'", settings.admin_username)
