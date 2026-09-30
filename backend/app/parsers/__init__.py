"""Parser registry - dispatch uploads to the right parser by detected kind."""

from __future__ import annotations

from app.parsers.base import (
    ColumnDetectionError,
    EncryptedPdfError,
    ParsedRow,
    ParsedStatement,
    ParserError,
    StatementParser,
    sniff_account_details,
    sniff_bank_name,
    sniff_opening_balance,
)
from app.parsers.csv_parser import CsvParser
from app.parsers.excel_parser import ExcelParser
from app.parsers.pdf_parser import PdfParser
from app.parsers.reconcile import has_money, reconcile_directions

_PARSERS: dict[str, type] = {
    "csv": CsvParser,
    "xlsx": ExcelParser,
    "xls": ExcelParser,
    "pdf": PdfParser,
}

__all__ = [
    "ColumnDetectionError",
    "EncryptedPdfError",
    "ParsedRow",
    "ParsedStatement",
    "ParserError",
    "StatementParser",
    "parse_statement",
    "sniff_opening_balance",
]


def parse_statement(
    kind: str,
    data: bytes,
    *,
    filename: str = "",
    password: str | None = None,
) -> ParsedStatement:
    """Parse raw statement bytes.

    `password` is used only for this call and is never stored or logged.
    """
    parser_cls = _PARSERS.get((kind or "").lower())
    if parser_cls is None:
        raise ParserError(f"Unsupported statement type: {kind or 'unknown'}")

    statement: ParsedStatement = parser_cls().parse(
        data, filename=filename, password=password
    )

    # The running balance is the authority on direction: it corrects swapped
    # Withdrawal/Deposit columns, restores amounts read as empty, and drops
    # rows that carry no money at all.
    reconcile_directions(
        statement.rows,
        statement.warnings,
        opening_balance=statement.opening_balance,
    )
    amountless = sum(1 for row in statement.rows if not has_money(row))
    statement.rows = [row for row in statement.rows if has_money(row)]
    if amountless:
        statement.warnings.append(
            f"Skipped {amountless} row(s) with no readable amount."
        )

    if not statement.rows:
        raise ColumnDetectionError(
            "No transactions could be extracted from the file. "
            "The statement may use an unsupported layout."
        )
    return statement
