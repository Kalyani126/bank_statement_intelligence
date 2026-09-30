"""Test bootstrap.

DATABASE_URL is redirected to an isolated *_test PostgreSQL database (or a
SQLite file when PostgreSQL is unreachable) BEFORE any app module imports,
so the engine is bound to test storage and the development data is never
touched.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_DIR / ".env")

# Fixed test settings (secrets stay backend-side either way).
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin123"
os.environ["AUTH_ENABLED"] = "true"
os.environ["JEV_ENABLED"] = "false"
os.environ["RAG_ENABLED"] = "true"
os.environ["LLM_PROVIDER"] = "none"
os.environ["EMBEDDING_PROVIDER"] = "local"


def _prepare_database_url() -> str:
    base = os.environ.get("DATABASE_URL", "")
    if not base.startswith(("postgresql://", "postgres://")):
        return f"sqlite:///{BACKEND_DIR / 'test_bank_statement.db'}"

    test_name = "bank_statement_db_test"
    scheme, _, remainder = base.partition("//")
    authority, _, _ = remainder.partition("/")
    admin_url = f"{scheme}//{authority}/postgres"
    test_url = f"{scheme}//{authority}/{test_name}"

    try:
        import sqlalchemy as sa

        engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with engine.connect() as conn:
            exists = conn.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": test_name},
            ).scalar()
            if not exists:
                conn.execute(sa.text(f'CREATE DATABASE "{test_name}"'))
        engine.dispose()
        return test_url
    except Exception as error:  # pragma: no cover - fallback for CI without PG
        print(f"PostgreSQL unavailable for tests ({type(error).__name__}); using SQLite")
        return f"sqlite:///{BACKEND_DIR / 'test_bank_statement.db'}"


os.environ["DATABASE_URL"] = _prepare_database_url()

from app.database.connection import engine  # noqa: E402
from app.models import Base  # noqa: E402

# Fresh schema for the whole pytest session (isolated test database).
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)

from app.database.connection import init_db  # noqa: E402

init_db()  # seed the admin user (unit tests do not boot the app lifespan)

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def test_app():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="session")
def auth_headers(test_app):
    response = test_app.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "admin123"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture(autouse=True)
def clean_tables():
    """Every test starts from empty business tables (admin user is kept)."""
    from app.database.connection import SessionLocal
    from app.models import (
        AuditLog,
        Person,
        PersonIdentifier,
        ProcessingJob,
        RagDocument,
        Statement,
        StatementFile,
        Transaction,
        TransactionPersonMatch,
    )

    with SessionLocal() as session:
        for model in (
            RagDocument,
            TransactionPersonMatch,
            Transaction,
            ProcessingJob,
            StatementFile,
            Statement,
            PersonIdentifier,
            Person,
            AuditLog,
        ):
            session.query(model).delete()
        session.commit()
    yield


@pytest.fixture()
def db():
    from app.database.connection import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def settings_obj():
    from app.config import get_settings

    return get_settings()


# --------------------------------------------------------------------------
# Helpers shared by tests
# --------------------------------------------------------------------------

STATEMENT_CSV = """Date,Narration,Ref No.,Withdrawal (INR),Deposit (INR),Closing Balance
01-09-2025,UPI/KALYAN/kalyan@okaxis/12345,12345,5000.00,,15000.00
05-09-2025,KALYAN KUMAR / UPI / kalyan@okaxis,UPIREF001,3000.00,,12000.00
10-09-2025,NEFT-KALYAN KUMAR-XXXX1234,NEFT0905,1500.00,,10500.00
15-09-2025,PAYTM-KALYAN-XXXX1234,PAYTM125,2000.00,,8500.00
20-09-2025,UPI/KALYAN/kalyan@okaxis/12347,12347,7500.00,,1000.00
22-09-2025,UPI/KALYAN KUMAR/kalyan@oksbi/555001,555001,900.00,,100.00
10-09-2025,RAVI KUMAR / ravi@okaxis,UPIRAVI1,2500.00,,0.00
15-09-2025,RAVI PAVAN / ravipavan@ybl,UPIRAVP1,750.00,,0.00
25-09-2025,RAVI,RAVIREF1,100.00,,0.00
"""

# Mixed statement: withdrawals AND deposits (credits are asserted, not assumed).
CREDIT_STATEMENT_CSV = """Date,Narration,Ref No.,Withdrawal (INR),Deposit (INR),Closing Balance
01-09-2025,UPI/KALYAN/kalyan@okaxis/12345,12345,5000.00,,15000.00
05-09-2025,SALARY CREDIT FROM EMPLOYER,SAL001,,30000.00,45000.00
10-09-2025,NEFT-KALYAN KUMAR-XXXX1234,NEFT0905,1500.00,,43500.00
15-09-2025,REFUND AMAZON ORDER,AMZ123,,250.50,43750.50
"""


def upload_and_wait(
    client,
    headers: dict,
    content: bytes,
    filename: str,
    *,
    content_type: str = "text/csv",
    password: str | None = None,
    name: str | None = None,
    timeout: float = 30.0,
) -> dict:
    """Upload a file and poll the processing job until it finishes."""
    data: dict[str, str] = {}
    if name:
        data["name"] = name
    if password is not None:
        data["password"] = password
    response = client.post(
        "/api/v1/statements/upload",
        headers=headers,
        files={"file": (filename, content, content_type)},
        data=data,
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    job_id = payload["job"]["id"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/processing/{job_id}", headers=headers).json()
        if job["status"] in ("COMPLETED", "FAILED"):
            payload["job"] = job
            break
        time.sleep(0.1)
    else:  # pragma: no cover
        raise AssertionError("Processing job did not finish in time")

    statement_id = payload["statement"]["id"]
    detail = client.get(f"/api/v1/statements/{statement_id}", headers=headers).json()
    payload["statement"] = detail
    return payload


def upload_standard_csv(client, headers: dict) -> dict:
    return upload_and_wait(
        client,
        headers,
        STATEMENT_CSV.encode("utf-8"),
        "statement.csv",
        name="September Statement",
    )


def upload_credit_csv(client, headers: dict) -> dict:
    return upload_and_wait(
        client,
        headers,
        CREDIT_STATEMENT_CSV.encode("utf-8"),
        "mixed-statement.csv",
        name="Mixed Statement",
    )


def seed_person(
    db,
    canonical_name: str,
    *,
    upis: tuple[str, ...] = (),
    accounts: tuple[str, ...] = (),
) -> "object":
    from app.models.enums import IdentifierType, PersonSource
    from app.models.person import Person, PersonIdentifier
    from app.services.normalization import normalize_name

    count = db.query(Person).count()
    person = Person(
        code=f"P{count + 1:03d}",
        canonical_name=canonical_name,
        normalized_name=normalize_name(canonical_name) or canonical_name.lower(),
        source=PersonSource.MANUAL,
    )
    db.add(person)
    db.flush()
    for value in upis:
        db.add(
            PersonIdentifier(
                person_id=person.id,
                id_type=IdentifierType.UPI,
                value=value,
                normalized_value=value,
                source="manual",
            )
        )
    for value in accounts:
        db.add(
            PersonIdentifier(
                person_id=person.id,
                id_type=IdentifierType.ACCOUNT,
                value=value,
                normalized_value=value,
                source="manual",
            )
        )
    db.commit()
    db.refresh(person)
    return person


def make_transaction(
    db,
    description: str,
    *,
    debit=None,
    credit=None,
    transaction_date=None,
):
    """Create a statement + transaction directly (unit-test speed)."""
    from datetime import date

    from app.models.enums import PaymentMethod, TransactionType
    from app.models.statement import Statement
    from app.models.transaction import Transaction
    from app.models.user import User
    from app.services.normalization import normalize_description

    user = db.query(User).order_by(User.id).first()
    statement = Statement(
        user_id=user.id,
        name="unit-test",
        file_type="CSV",
        status="COMPLETED",
    )
    db.add(statement)
    db.flush()

    norm = normalize_description(description)
    if debit is not None:
        txn_type = TransactionType.DEBIT
    elif credit is not None:
        txn_type = TransactionType.CREDIT
    else:
        txn_type = TransactionType.UNKNOWN

    transaction = Transaction(
        statement_id=statement.id,
        transaction_date=transaction_date or date(2025, 9, 10),
        description=description,
        raw_description=description,
        narration=description,
        raw_text=description,
        debit_amount=debit,
        credit_amount=credit,
        transaction_type=txn_type,
        payment_method=(norm.payment_method or PaymentMethod.OTHER).value,
        upi_id=norm.upi_id,
        normalized_upi_id=norm.normalized_upi_id,
        counterparty_name=norm.name,
        normalized_counterparty_name=norm.normalized_name,
    )
    db.add(transaction)
    db.commit()
    db.refresh(transaction)
    return transaction, norm
