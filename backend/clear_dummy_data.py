"""One-off cleanup: remove all test/dummy statement data.

Keeps the users table (admin login). Deletes: audit logs, RAG documents,
statements (DB cascade removes files, transactions, jobs, matches), persons
(identifiers cascade), and every file in storage/uploads.

Run from backend/: venv/Scripts/python clear_dummy_data.py
"""

from pathlib import Path

from app.database.connection import SessionLocal
from app.models import AuditLog, Person, RagDocument, Statement

BACKEND_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = BACKEND_DIR / "storage" / "uploads"


def main() -> None:
    db = SessionLocal()
    try:
        counts = {
            "statements": db.query(Statement).count(),
            "persons": db.query(Person).count(),
            "rag_documents": db.query(RagDocument).count(),
            "audit_logs": db.query(AuditLog).count(),
        }

        db.query(AuditLog).delete(synchronize_session=False)
        db.query(RagDocument).delete(synchronize_session=False)
        db.query(Statement).delete(synchronize_session=False)
        db.query(Person).delete(synchronize_session=False)
        db.commit()

        removed_files = 0
        if UPLOADS_DIR.exists():
            for path in UPLOADS_DIR.iterdir():
                if path.is_file():
                    path.unlink()
                    removed_files += 1

        print("Deleted:", counts)
        print(f"Removed upload files: {removed_files}")
        print(
            "Remaining -> statements:",
            db.query(Statement).count(),
            "persons:",
            db.query(Person).count(),
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
