"""Flexible column detection for tabular statements.

Bank exports never share one fixed layout, so headers are matched against a
synonym table and - when no header exists at all - a positional heuristic is
used. Detection decisions are surfaced as warnings for traceability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.parsers.base import ParsedRow
from app.utils.dates import parse_date_any
from app.utils.money import parse_amount, parse_amount_with_side


@dataclass
class ColumnMap:
    date_idx: int
    value_date_idx: int | None = None
    description_idx: int | None = None
    reference_idx: int | None = None
    debit_idx: int | None = None
    credit_idx: int | None = None
    balance_idx: int | None = None
    amount_idx: int | None = None
    side_idx: int | None = None
    source: str = "header"  # header | positional


_HEADER_ALIASES: dict[str, set[str]] = {
    "date": {
        "date", "txndate", "trandate", "transactiondate", "postingdate",
        "postdate", "dt", "dated", "tran date", "txndt",
    },
    "value_date": {"valuedate", "valdate", "datedt", "valuedt"},
    "description": {
        "narration", "description", "particulars", "particular", "details",
        "transactiondetails", "transactionremarks", "remarks", "info",
        "narrative", "transactionparticulars", "particularsinformation",
        "transactiondescription", "transactionnarration", "tranparticulars",
    },
    "reference": {
        "reference", "ref", "refno", "referenceno", "utr", "utrno", "rrn",
        "chqno", "chequeno", "chqrefno", "chequerefno", "instrumentno",
        "instrumentnumber", "instno", "tranid", "transactionid", "txnid",
        "checkno", "chequeref", "refnochequeno", "refnocheque", "chequeref",
    },
    "debit": {
        "debit", "debits", "withdrawal", "withdrawals", "withdrawalamt",
        "withdrawalamount", "debitamount", "paidout", "dr", "amountdr",
        "debitsamount",
    },
    "credit": {
        "credit", "credits", "deposit", "deposits", "depositamt",
        "depositamount", "creditamount", "paidin", "cr", "amountcr",
        "creditsamount",
    },
    "balance": {
        "balance", "bal", "closingbalance", "runningbalance",
        "availablebalance", "closingbal",
    },
    "amount": {"amount", "amt", "txnamount", "transactionamount"},
    "side": {"drcr", "drcrtype", "indicator", "dr cr", "debitcredit", "sign"},
}

_SUFFIXES_TO_STRIP = ("inr", "rs", "amount", "amt", "value", "ind", "inr.")


def _normalize_header(cell: Any) -> str:
    if cell is None:
        return ""
    text = str(cell).strip().lower()
    text = re.sub(r"\(.*?\)", "", text)  # drop "(INR)" style notes
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text


def _match_field(key: str) -> str | None:
    if not key:
        return None
    for field_name, aliases in _HEADER_ALIASES.items():
        if key in aliases:
            return field_name
    # Strip common suffixes: "withdrawalinr" -> "withdrawal"
    for suffix in _SUFFIXES_TO_STRIP:
        if key.endswith(suffix) and len(key) > len(suffix):
            trimmed = key[: -len(suffix)]
            for field_name, aliases in _HEADER_ALIASES.items():
                if trimmed in aliases:
                    return field_name
    return None


def _looks_numeric(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, (int, float)):
        return True
    return parse_amount(str(value)) is not None


def find_header(rows: list[list[Any]], scan_limit: int = 30) -> tuple[int, ColumnMap] | None:
    for index, row in enumerate(rows[:scan_limit]):
        mapping: dict[str, int] = {}
        for col, cell in enumerate(row):
            field_name = _match_field(_normalize_header(cell))
            if field_name and field_name not in mapping:
                mapping[field_name] = col
        if "date" in mapping and (
            "debit" in mapping or "credit" in mapping or "amount" in mapping
        ):
            return index, ColumnMap(
                date_idx=mapping["date"],
                value_date_idx=mapping.get("value_date"),
                description_idx=mapping.get("description"),
                reference_idx=mapping.get("reference"),
                debit_idx=mapping.get("debit"),
                credit_idx=mapping.get("credit"),
                balance_idx=mapping.get("balance"),
                amount_idx=mapping.get("amount"),
                side_idx=mapping.get("side"),
                source="header",
            )
    return None


def detect_positional(rows: list[list[Any]], scan_limit: int = 30) -> ColumnMap | None:
    """Headerless fallback: date in the first columns, amounts trailing."""
    sample = [row for row in rows[:scan_limit] if row and any(cell not in (None, "") for cell in row)]
    if not sample:
        return None

    # Step 1: Detect transaction date column (col 0 or 1)
    date_idx = None
    for candidate in (0, 1):
        hits = sum(
            1
            for row in sample
            if len(row) > candidate
            and isinstance(row[candidate], (str, object))
            and parse_date_any(row[candidate]) is not None
            and not _looks_numeric(row[candidate])
        )
        if hits >= max(1, len(sample) // 2):
            date_idx = candidate
            break
    if date_idx is None:
        return None

    # Step 1b: Detect if the subsequent column is also a date (Value Date)
    value_date_idx = None
    next_col = date_idx + 1
    vdate_hits = sum(
        1
        for row in sample
        if len(row) > next_col
        and isinstance(row[next_col], (str, object))
        and parse_date_any(row[next_col]) is not None
        and not _looks_numeric(row[next_col])
    )
    if vdate_hits >= max(1, len(sample) // 2):
        value_date_idx = next_col

    width = max(len(row) for row in sample)
    data_start_col = (value_date_idx if value_date_idx is not None else date_idx) + 1

    # Step 2: Identify candidate amount / numeric columns
    BLANK_TOKENS = {None, "", "-", "--", "---", "n/a", "na", "0.00", "0"}
    numeric_cols: list[int] = []
    for col in range(data_start_col, width):
        values = [row[col] for row in sample if len(row) > col]
        non_blank = [
            v for v in values
            if v not in BLANK_TOKENS and str(v).strip().lower() not in BLANK_TOKENS
        ]
        if non_blank:
            num_hits = sum(1 for v in non_blank if _looks_numeric(v))
            is_date_col = sum(1 for v in non_blank if not _looks_numeric(v) and parse_date_any(v) is not None) >= 0.5 * len(non_blank)
            if not is_date_col and num_hits >= 0.75 * len(non_blank):
                numeric_cols.append(col)

    # Step 3: Text columns between dates and numeric block
    text_cols = [
        col
        for col in range(data_start_col, numeric_cols[0] if numeric_cols else width)
        if col not in numeric_cols
    ]

    # Pick description as the text column with highest average string length
    if len(text_cols) == 1:
        description_idx = text_cols[0]
        reference_idx = None
    elif len(text_cols) > 1:
        def avg_str_len(c: int) -> float:
            cells = [str(r[c]) for r in sample if len(r) > c and str(r[c]).strip() not in BLANK_TOKENS]
            return sum(len(x) for x in cells) / max(1, len(cells))
        sorted_text = sorted(text_cols, key=avg_str_len, reverse=True)
        description_idx = sorted_text[0]
        reference_idx = sorted_text[1]
    else:
        description_idx = None
        reference_idx = None

    # Step 4: Map amount columns
    # Layout A: 3 numeric columns (Debit | Credit | Balance)
    if len(numeric_cols) >= 3:
        return ColumnMap(
            date_idx=date_idx,
            value_date_idx=value_date_idx,
            description_idx=description_idx,
            reference_idx=reference_idx,
            debit_idx=numeric_cols[-3],
            credit_idx=numeric_cols[-2],
            balance_idx=numeric_cols[-1],
            source="positional",
        )

    # Layout B: 6+ column statements (e.g. SBI) where one of Debit/Credit had all dashes in sample
    # E.g. [Date, Value Date, Description, Ref, Debit, Credit, Balance]
    if width >= 6 and len(numeric_cols) == 2:
        last_col = width - 1
        col_b = width - 2  # Credit
        col_a = width - 3  # Debit
        if last_col in numeric_cols and (col_a in numeric_cols or col_b in numeric_cols):
            return ColumnMap(
                date_idx=date_idx,
                value_date_idx=value_date_idx,
                description_idx=description_idx,
                reference_idx=reference_idx,
                debit_idx=col_a,
                credit_idx=col_b,
                balance_idx=last_col,
                source="positional",
            )

    # Layout C: Amount + Balance (2 numeric columns)
    if len(numeric_cols) == 2:
        return ColumnMap(
            date_idx=date_idx,
            value_date_idx=value_date_idx,
            description_idx=description_idx,
            reference_idx=reference_idx,
            amount_idx=numeric_cols[0],
            balance_idx=numeric_cols[1],
            source="positional",
        )

    # Layout D: Single amount column
    if len(numeric_cols) == 1:
        return ColumnMap(
            date_idx=date_idx,
            value_date_idx=value_date_idx,
            description_idx=description_idx,
            reference_idx=reference_idx,
            amount_idx=numeric_cols[0],
            source="positional",
        )

    return None


def _cell(row: list[Any], idx: int | None) -> Any:
    if idx is None or idx >= len(row):
        return None
    value = row[idx]
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip() or None


def build_row(row: list[Any], column_map: ColumnMap, source_page: int | None = None) -> ParsedRow:
    transaction_date = parse_date_any(_cell(row, column_map.date_idx))
    value_date = parse_date_any(_cell(row, column_map.value_date_idx)) if column_map.value_date_idx is not None else None

    description_parts: list[str] = []
    if column_map.description_idx is not None:
        text = _text(_cell(row, column_map.description_idx))
        if text:
            description_parts.append(text)
    if column_map.reference_idx is not None:
        ref_text = _text(_cell(row, column_map.reference_idx))
        # Reference column often doubles as narration in positional mode.
        if ref_text and column_map.description_idx is None:
            description_parts.append(ref_text)
    description = " ".join(description_parts) or None
    reference = _text(_cell(row, column_map.reference_idx)) if column_map.description_idx is not None else None

    # Safety net: If description happens to be a pure date, but reference has substantive text, swap them
    if description and parse_date_any(description) is not None:
        if reference and parse_date_any(reference) is None and len(reference.strip()) > 3:
            value_date = value_date or parse_date_any(description)
            description, reference = reference, None

    debit = credit = balance = None

    if column_map.debit_idx is not None:
        debit = parse_amount(_cell(row, column_map.debit_idx))
    if column_map.credit_idx is not None:
        credit = parse_amount(_cell(row, column_map.credit_idx))

    if column_map.amount_idx is not None:
        amount, side = parse_amount_with_side(_cell(row, column_map.amount_idx))
        if column_map.side_idx is not None:
            side_cell = _text(_cell(row, column_map.side_idx))
            if side_cell:
                lowered = side_cell.lower()
                if lowered.startswith(("d", "w")):
                    side = "DEBIT"
                elif lowered.startswith(("c", "r")):
                    side = "CREDIT"
        if amount is not None:
            if not side and description:
                desc_upper = description.upper()
                if any(w in desc_upper for w in ("WDL", "WITHDRAWAL", "DR/", "/DR/", "DEBIT", "PAID TO", "TRANSFER TO")):
                    side = "DEBIT"
                elif any(w in desc_upper for w in ("DEP", "DEPOSIT", "CR/", "/CR/", "CREDIT", "RECEIVED FROM")):
                    side = "CREDIT"

            if side == "DEBIT":
                debit = amount
            elif side == "CREDIT":
                credit = amount
            elif amount < 0:
                debit = -amount
            else:
                credit = amount

    if column_map.balance_idx is not None:
        balance = parse_amount(_cell(row, column_map.balance_idx))

    raw_text = " | ".join(
        str(cell).strip()
        for cell in row
        if cell is not None and str(cell).strip() != ""
    )

    return ParsedRow(
        transaction_date=transaction_date,
        value_date=value_date,
        description=description,
        reference=reference,
        debit=debit,
        credit=credit,
        balance=balance,
        raw_text=raw_text,
        source_page=source_page,
    )


def is_transaction_row(row: ParsedRow) -> bool:
    """Rows without a date and without amounts are headers/totals, not data."""
    has_money = row.debit is not None or row.credit is not None
    return row.transaction_date is not None and has_money


def is_candidate_row(row: ParsedRow) -> bool:
    """Kept for reconciliation: a dated row with money OR a running balance.

    An amount cell that the layout read as empty must survive until the
    running balance can restore it (see app.parsers.reconcile).
    """
    if row.transaction_date is None:
        return False
    return (
        row.debit is not None
        or row.credit is not None
        or row.balance is not None
    )


def rows_to_parsed(
    rows: list[list[Any]],
    warnings: list[str],
    source_page: int | None = None,
    column_map: ColumnMap | None = None,
) -> list[ParsedRow]:
    if column_map is None:
        header = find_header(rows)
        if header:
            header_index, column_map = header
            data_rows = rows[header_index + 1 :]
        else:
            column_map = detect_positional(rows)
            if column_map is None:
                return []
            data_rows = rows
            warnings.append(
                "No recognisable header row found; used positional column detection."
            )
    else:
        # Check if this batch of rows starts with its own header
        header = find_header(rows)
        if header:
            header_index, column_map = header
            data_rows = rows[header_index + 1 :]
        else:
            data_rows = rows

    parsed: list[ParsedRow] = []
    for row in data_rows:
        if not row or all(cell in (None, "") for cell in row):
            continue
        item = build_row(row, column_map, source_page=source_page)
        if is_candidate_row(item):
            parsed.append(item)
    return parsed
