"""Upload validation and secure temporary file handling."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings

logger = logging.getLogger(__name__)

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"

# Uploads that are small, random-named and live only for re-processing.
_MAX_ORIGINAL_NAME = 200


class FileValidationError(ValueError):
    """Safe-to-display upload validation error (never contains secrets)."""


class DuplicateUploadError(FileValidationError):
    """The exact same file is already stored for this account.

    Carries the existing statement id so the API can answer with 409 and
    point the user at it instead of double-counting their data.
    """

    def __init__(self, message: str, statement_id: int) -> None:
        super().__init__(message)
        self.statement_id = statement_id


@dataclass
class StoredFile:
    path: Path
    original_filename: str
    stored_filename: str
    size: int
    sha256: str
    kind: str  # pdf | xlsx | xls | csv


def sanitize_filename(filename: str) -> str:
    name = os.path.basename(filename or "upload")
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)
    return name[:_MAX_ORIGINAL_NAME] or "upload"


def detect_kind(extension: str, head_bytes: bytes) -> str | None:
    """Cross-check extension against magic bytes; return kind or None."""
    ext = extension.lower()
    if ext == ".pdf":
        return "pdf" if head_bytes.startswith(_PDF_MAGIC) else None
    if ext in (".xlsx", ".xlsm"):
        return "xlsx" if head_bytes.startswith(_ZIP_MAGIC) else None
    if ext == ".xls":
        if head_bytes.startswith(_OLE2_MAGIC):
            return "xls"
        # Some tools export HTML/XML tables with an .xls name.
        sample = head_bytes[:512].lstrip().lower()
        if sample.startswith((b"<html", b"<?xml", b"<table")):
            return "xls"
        return None
    if ext == ".csv":
        if head_bytes.startswith((_PDF_MAGIC, _ZIP_MAGIC, _OLE2_MAGIC)):
            return None
        try:
            head_bytes.decode("utf-8")
        except UnicodeDecodeError:
            try:
                head_bytes.decode("latin-1")
            except UnicodeDecodeError:
                return None
        return "csv"
    return None


def validate_upload(
    filename: str,
    size: int,
    head_bytes: bytes,
    settings: Settings,
) -> str:
    """Validate an upload; returns the detected kind. Raises FileValidationError."""
    original = sanitize_filename(filename)
    ext = os.path.splitext(original)[1].lower()

    if ext not in settings.allowed_extension_list:
        raise FileValidationError(
            f"Unsupported file type '{ext or 'unknown'}'. Allowed: "
            + ", ".join(settings.allowed_extension_list)
        )
    if size <= 0:
        raise FileValidationError("The uploaded file is empty.")
    if size > settings.max_upload_bytes:
        raise FileValidationError(
            f"File exceeds the {settings.max_upload_size_mb} MB upload limit."
        )

    kind = detect_kind(ext, head_bytes)
    if kind is None:
        raise FileValidationError(
            "File content does not match its extension or the file is corrupt."
        )
    return kind


def save_upload(upload_dir: str, filename: str, chunks: list[bytes]) -> StoredFile:
    """Persist the upload under a random name; original name only in DB."""
    directory = Path(upload_dir)
    directory.mkdir(parents=True, exist_ok=True)

    original = sanitize_filename(filename)
    ext = os.path.splitext(original)[1].lower()
    stored_name = f"{uuid.uuid4().hex}{ext}"
    path = directory / stored_name

    digest = hashlib.sha256()
    size = 0
    with open(path, "wb") as handle:
        for chunk in chunks:
            digest.update(chunk)
            size += len(chunk)
            handle.write(chunk)

    # Best-effort restrictive permissions on POSIX; no-op on Windows.
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass

    head = b""
    with open(path, "rb") as handle:
        head = handle.read(16)

    kind = detect_kind(ext, head)
    if kind is None:
        path.unlink(missing_ok=True)
        raise FileValidationError("File content does not match its extension.")

    return StoredFile(
        path=path,
        original_filename=original,
        stored_filename=stored_name,
        size=size,
        sha256=digest.hexdigest(),
        kind=kind,
    )


def delete_stored_file(stored_path: str) -> None:
    try:
        Path(stored_path).unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        logger.warning("Could not delete stored file")
