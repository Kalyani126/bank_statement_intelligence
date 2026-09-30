"""CSV statement parser with encoding/dialect/column autodetection."""

from __future__ import annotations

import csv
import io
import re

from app.parsers.base import (
    ParsedStatement,
    ParserError,
    sniff_account_details,
    sniff_bank_name,
    sniff_opening_balance,
)
from app.parsers.detector import rows_to_parsed

_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
_EMPTY_ROW = re.compile(r"^\s*$")


def decode_bytes(data: bytes) -> str:
    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _split_rows(text: str) -> list[list[str]]:
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    return [row for row in reader]


class CsvParser:
    def parse(
        self,
        data: bytes,
        *,
        filename: str = "",
        password: str | None = None,
    ) -> ParsedStatement:
        text = decode_bytes(data)
        try:
            rows = _split_rows(text)
        except csv.Error as error:
            raise ParserError("The CSV file could not be read.") from error

        rows = [row for row in rows if any(not _EMPTY_ROW.match(str(c or "")) for c in row)]
        if not rows:
            raise ParserError("The CSV file contains no data rows.")

        warnings: list[str] = []
        parsed = rows_to_parsed(rows, warnings)

        holder, account = sniff_account_details(text[:4000])
        lines = text.splitlines()
        first_data = next(
            (
                index
                for index, line in enumerate(lines)
                if re.match(r"^\s*\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b", line)
            ),
            0,
        )
        return ParsedStatement(
            rows=parsed,
            bank_name=sniff_bank_name(text[:4000]),
            account_holder=holder,
            account_number_raw=account,
            warnings=warnings,
            opening_balance=sniff_opening_balance("\n".join(lines[:first_data])),
        )
