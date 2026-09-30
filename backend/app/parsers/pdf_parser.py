"""PDF statement parser - table extraction first, text-line fallback second.

Password-protected PDFs are supported: the password is only ever held in
memory for this single extraction call - never persisted, never logged.
"""

from __future__ import annotations

import io
import re
from decimal import Decimal
from typing import Any

from app.parsers.base import (
    ColumnDetectionError,
    EncryptedPdfError,
    ParsedRow,
    ParsedStatement,
    ParserError,
    sniff_account_details,
    sniff_bank_name,
    sniff_opening_balance,
)
from app.parsers.detector import ColumnMap, detect_positional, find_header, rows_to_parsed
from app.utils.dates import parse_date_any
from app.utils.money import parse_amount, parse_amount_with_side

_DATE_TOKEN = r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
_DATE_LINE_RE = re.compile(
    rf"^\s*(?P<date>{_DATE_TOKEN})(?:\s+(?P<vdate>{_DATE_TOKEN}))?\s+(?P<rest>\S.*)$"
)
_MONEY_TOKEN = re.compile(
    r"(?:₹|Rs\.?\s*)?-?[\d,]+\.\d{2}-?(?:\s*(?:Dr|Cr))?", re.IGNORECASE
)


def _trailing_money_tokens(text: str) -> tuple[str, list[str]]:
    """Split 'NARRATION 500.00 12,000.00' -> ('NARRATION', [...])."""
    stripped = text.rstrip()
    matches = list(_MONEY_TOKEN.finditer(stripped))
    trailing: list[str] = []
    limit = len(stripped)
    for match in reversed(matches):
        gap = stripped[match.end() : limit]
        if match.end() <= limit and not gap.strip():
            trailing.append(match.group(0))
            limit = match.start()
        else:
            break
    trailing.reverse()
    description = stripped[:limit].strip(" -|:") if limit else stripped
    return description, trailing


def _classify_amounts(
    tokens: list[str], warnings: list[str]
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Map 1-3 trailing money tokens to (debit, credit, balance)."""
    if not tokens:
        return None, None, None

    if len(tokens) >= 3:
        first, side_first = parse_amount_with_side(tokens[0])
        second, side_second = parse_amount_with_side(tokens[1])
        balance = parse_amount(tokens[2])
        if side_first or side_second:
            debit = first if side_first == "DEBIT" else (second if side_second == "DEBIT" else None)
            credit = first if side_first == "CREDIT" else (second if side_second == "CREDIT" else None)
            return debit, credit, balance
        # Convention: Withdrawal | Deposit | Balance (warned once).
        if "withdrawal/deposit order assumed for PDF amounts" not in warnings:
            warnings.append("withdrawal/deposit order assumed for PDF amounts")
        return first, second, balance

    if len(tokens) == 2:
        amount, side = parse_amount_with_side(tokens[0])
        balance = parse_amount(tokens[1])
        if side == "DEBIT":
            return amount, None, balance
        if side == "CREDIT":
            return None, amount, balance
        if amount is not None and amount < 0:
            return -amount, None, balance
        if "unsigned PDF amount treated as credit" not in warnings:
            warnings.append("unsigned PDF amount treated as credit")
        return None, amount, balance

    amount, side = parse_amount_with_side(tokens[0])
    if side == "DEBIT":
        return amount, None, None
    if side == "CREDIT":
        return None, amount, None
    if amount is not None and amount < 0:
        return -amount, None, None
    if "unsigned PDF amount treated as credit" not in warnings:
        warnings.append("unsigned PDF amount treated as credit")
    return None, amount, None


def _parse_text_lines(
    lines: list[str], warnings: list[str], source_page: int | None
) -> list[ParsedRow]:
    rows: list[ParsedRow] = []
    pending: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        debit, credit, balance = _classify_amounts(pending["tokens"], warnings)
        raw_text = " | ".join(pending["raw_parts"])
        rows.append(
            ParsedRow(
                transaction_date=pending["date"],
                value_date=pending["vdate"],
                description=pending["description"] or None,
                debit=debit,
                credit=credit,
                balance=balance,
                raw_text=raw_text,
                source_page=source_page,
            )
        )
        pending = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        match = _DATE_LINE_RE.match(stripped)
        if match:
            flush()
            description, tokens = _trailing_money_tokens(match.group("rest"))
            pending = {
                "date": parse_date_any(match.group("date")),
                "vdate": parse_date_any(match.group("vdate")),
                "description": description,
                "tokens": tokens,
                "raw_parts": [stripped],
            }
        elif pending is not None:
            # Continuation of the previous transaction's narration.
            extra_description, extra_tokens = _trailing_money_tokens(stripped)
            if extra_description:
                pending["description"] = (
                    f"{pending['description']} {extra_description}".strip()
                )
            if extra_tokens:
                pending["tokens"].extend(extra_tokens)
            pending["raw_parts"].append(stripped)
        # Lines before the first date line (headers/legends) are ignored.
    flush()
    return rows


def _is_password_error(error: Exception) -> bool:
    """Detect password errors anywhere in the exception chain.

    pdfplumber wraps pdfminer's PDFPasswordIncorrect in a generic
    PdfminerException whose message may be empty, so names of the whole
    chain are inspected.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if "password" in type(current).__name__.lower():
            return True
        if "password" in str(current).lower():
            return True
        current = current.__cause__ or current.__context__
    return False


class PdfParser:
    def parse(
        self,
        data: bytes,
        *,
        filename: str = "",
        password: str | None = None,
    ) -> ParsedStatement:
        warnings: list[str] = []
        rows: list[ParsedRow] = []
        header_text = ""

        rows, header_text = self._with_pdfplumber(data, password, warnings)
        if not rows:
            fallback_rows, fallback_text = self._with_pymupdf(data, password, warnings)
            if fallback_rows:
                rows = fallback_rows
                header_text = header_text or fallback_text

        if not rows:
            raise ColumnDetectionError(
                "No transactions could be extracted from the PDF. "
                "It may be a scanned image or use an unsupported layout."
            )

        holder, account = sniff_account_details(header_text)
        header_lines = header_text.splitlines()
        cut = next(
            (
                index
                for index, line in enumerate(header_lines)
                if re.match(r"^\s*\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b", line)
            ),
            len(header_lines),
        )
        return ParsedStatement(
            rows=rows,
            bank_name=sniff_bank_name(header_text),
            account_holder=holder,
            account_number_raw=account,
            warnings=warnings,
            opening_balance=sniff_opening_balance("\n".join(header_lines[:cut])),
        )

    # --- pdfplumber (primary) ----------------------------------------------
    def _with_pdfplumber(
        self, data: bytes, password: str | None, warnings: list[str]
    ) -> tuple[list[ParsedRow], str]:
        try:
            import pdfplumber
        except ImportError:  # pragma: no cover - dependency guarded
            return [], ""

        try:
            pdf = pdfplumber.open(io.BytesIO(data), password=password)
        except Exception as error:
            if _is_password_error(error):
                raise EncryptedPdfError() from error
            raise ParserError("The PDF file could not be opened.") from error

        rows: list[ParsedRow] = []
        page_texts: list[tuple[int, str]] = []
        header_text = ""
        active_map: ColumnMap | None = None
        try:
            for page_number, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                page_texts.append((page_number, text))
                if len(header_text) < 5000:
                    header_text += text[:5000]

                for table in page.extract_tables() or []:
                    matrix = [[cell for cell in row] for row in table]
                    hdr = find_header(matrix)
                    if hdr:
                        _, active_map = hdr
                    elif active_map is None:
                        active_map = detect_positional(matrix)

                    rows.extend(
                        rows_to_parsed(
                            matrix,
                            warnings,
                            source_page=page_number,
                            column_map=active_map,
                        )
                    )
        finally:
            pdf.close()

        if not rows:
            for page_number, text in page_texts:
                rows.extend(_parse_text_lines(text.splitlines(), warnings, page_number))

        return rows, header_text

    # --- pymupdf (fallback) -------------------------------------------------
    def _with_pymupdf(
        self, data: bytes, password: str | None, warnings: list[str]
    ) -> tuple[list[ParsedRow], str]:
        try:
            import fitz  # pymupdf
        except ImportError:  # pragma: no cover
            return [], ""

        try:
            document = fitz.open(stream=data, filetype="pdf")
        except Exception as error:
            if _is_password_error(error):
                raise EncryptedPdfError() from error
            return [], ""

        rows: list[ParsedRow] = []
        header_text = ""
        active_map: ColumnMap | None = None
        try:
            if document.needs_pass and not document.authenticate(password or ""):
                raise EncryptedPdfError()

            for page_number, page in enumerate(document, start=1):
                text = page.get_text() or ""
                if len(header_text) < 5000:
                    header_text += text[:5000]
                try:
                    finder = page.find_tables()
                    for table in finder.tables:
                        matrix = table.extract()
                        hdr = find_header(matrix)
                        if hdr:
                            _, active_map = hdr
                        elif active_map is None:
                            active_map = detect_positional(matrix)

                        rows.extend(
                            rows_to_parsed(
                                matrix,
                                warnings,
                                source_page=page_number,
                                column_map=active_map,
                            )
                        )
                except Exception:  # table finder is best-effort
                    pass
                if not rows:
                    rows.extend(
                        _parse_text_lines(text.splitlines(), warnings, page_number)
                    )
        finally:
            document.close()

        return rows, header_text
