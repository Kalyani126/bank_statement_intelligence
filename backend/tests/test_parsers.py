"""Parser tests: CSV/XLSX/PDF extraction incl. password-protected PDFs."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pytest

from app.parsers import ColumnDetectionError, EncryptedPdfError, parse_statement
from app.parsers.csv_parser import CsvParser
from app.parsers.excel_parser import ExcelParser

from tests.conftest import STATEMENT_CSV, upload_and_wait


def test_csv_parses_hdfc_style_header():
    parsed = parse_statement(
        "csv", STATEMENT_CSV.encode("utf-8"), filename="statement.csv"
    )
    assert len(parsed.rows) == 9
    first = parsed.rows[0]
    assert first.transaction_date == date(2025, 9, 1)
    assert first.debit == Decimal("5000.00")
    assert first.credit is None
    assert first.balance == Decimal("15000.00")
    assert "kalyan@okaxis" in (first.description or "")
    assert first.raw_text  # raw preserved


def test_csv_headerless_positional_layout():
    content = """01-09-2025,UPI/KALYAN/kalyan@okaxis/12345,5000.00,,15000.00
02-09-2025,NEFT KUMAR XXXX1234,,3000.00,18000.00
"""
    parsed = parse_statement("csv", content.encode("utf-8"), filename="raw.csv")
    assert len(parsed.rows) == 2
    assert parsed.rows[0].debit == Decimal("5000.00")
    assert parsed.rows[1].credit == Decimal("3000.00")
    assert parsed.warnings  # positional detection is reported


def test_xlsx_parses_worksheet():
    openpyxl = pytest.importorskip("openpyxl")

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    rows = [
        ["Date", "Narration", "Ref No.", "Withdrawal (INR)", "Deposit (INR)", "Closing Balance"],
        [date(2025, 9, 1), "UPI/KALYAN/kalyan@okaxis/12345", "12345", 5000.0, None, 15000.0],
        [date(2025, 9, 5), "PAYTM-KALYAN-XXXX1234", "PAYTM1", 2000.0, None, 13000.0],
    ]
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)

    parsed = parse_statement("xlsx", buffer.getvalue(), filename="statement.xlsx")
    assert len(parsed.rows) == 2
    assert parsed.rows[0].debit == Decimal("5000.00")
    assert parsed.rows[0].transaction_date == date(2025, 9, 1)


def test_garbage_file_is_rejected_with_clear_message():
    with pytest.raises(ColumnDetectionError):
        parse_statement("csv", b"not,a,statement\njust,some,text\n", filename="x.csv")


def _make_pdf(lines: list[str], password: str | None = None) -> bytes:
    fitz = pytest.importorskip("fitz")  # pymupdf

    document = fitz.open()
    page = document.new_page()
    y = 60
    for line in lines:
        page.insert_text((50, y), line, fontsize=11, fontname="helv")
        y += 18
    kwargs: dict = {}
    if password:
        kwargs = {
            "encryption": fitz.PDF_ENCRYPT_AES_256,
            "user_pw": password,
            "owner_pw": password + "-owner",
        }
    data = document.tobytes(**kwargs)
    document.close()
    return data


def test_plain_pdf_text_lines_are_parsed():
    pdf = _make_pdf(
        [
            "01-09-2025 UPI/KALYAN/kalyan@okaxis/12345 5000.00 Dr 15000.00",
            "05-09-2025 NEFT-KALYAN KUMAR-XXXX1234 1500.00 Dr 13500.00",
        ]
    )
    parsed = parse_statement("pdf", pdf, filename="statement.pdf")
    assert len(parsed.rows) == 2
    assert parsed.rows[0].debit == Decimal("5000.00")
    assert parsed.rows[0].description and "kalyan@okaxis" in parsed.rows[0].description


def test_encrypted_pdf_without_password_raises_and_never_leaks_it():
    password = "S3cret-Pdf-Pass!"
    pdf = _make_pdf(["01-09-2025 UPI/KALYAN/kalyan@okaxis/12345 5000.00 Dr 15000.00"], password=password)

    with pytest.raises(EncryptedPdfError) as error:
        parse_statement("pdf", pdf, filename="locked.pdf")
    assert password not in str(error.value)


def test_encrypted_pdf_with_password_parses():
    password = "S3cret-Pdf-Pass!"
    pdf = _make_pdf(
        ["01-09-2025 UPI/KALYAN/kalyan@okaxis/12345 5000.00 Dr 15000.00"],
        password=password,
    )
    parsed = parse_statement("pdf", pdf, filename="locked.pdf", password=password)
    assert len(parsed.rows) == 1
    assert parsed.rows[0].debit == Decimal("5000.00")


# --------------------------------------------------------------------------
# Running-balance reconciliation: credits must never be lost or swapped
# --------------------------------------------------------------------------

def test_swapped_deposit_withdrawal_columns_are_corrected():
    # Headerless layout where Deposit comes before Withdrawal: the heuristic
    # guesses wrong, the running balance proves it and fixes every row.
    content = """01-09-2025,UPI/KALYAN/kalyan@okaxis/12345,,5000.00,15000.00
05-09-2025,SALARY CREDIT FROM EMPLOYER,30000.00,,45000.00
10-09-2025,PAYTM-KALYAN-XXXX1234,,2000.00,43000.00
"""
    parsed = parse_statement("csv", content.encode("utf-8"), filename="raw.csv")

    assert [row.debit for row in parsed.rows] == [
        Decimal("5000.00"), None, Decimal("2000.00")
    ]
    assert [row.credit for row in parsed.rows] == [
        None, Decimal("30000.00"), None
    ]
    assert any("corrected the direction" in w for w in parsed.warnings)


def test_deposit_row_with_empty_amount_is_recovered_from_balance():
    # The deposit cell read as empty: the row must survive (as a CREDIT),
    # otherwise the statement silently loses every credit.
    content = """Date,Narration,Withdrawal (INR),Deposit (INR),Closing Balance
01-09-2025,UPI/KALYAN/kalyan@okaxis/12345,5000.00,,15000.00
05-09-2025,SALARY CREDIT FROM EMPLOYER,,,45000.00
10-09-2025,PAYTM-KALYAN-XXXX1234,2000.00,,43000.00
"""
    parsed = parse_statement("csv", content.encode("utf-8"), filename="x.csv")

    assert len(parsed.rows) == 3
    assert parsed.rows[1].credit == Decimal("30000.00")
    assert parsed.rows[1].debit is None
    assert any("Recovered" in w for w in parsed.warnings)


def test_zero_placeholder_cells_do_not_make_every_row_a_debit():
    content = """Date,Narration,Withdrawal (INR),Deposit (INR),Closing Balance
01-09-2025,UPI/KALYAN/kalyan@okaxis/12345,5000.00,0.00,15000.00
05-09-2025,SALARY CREDIT FROM EMPLOYER,0.00,30000.00,45000.00
"""
    parsed = parse_statement("csv", content.encode("utf-8"), filename="x.csv")

    assert parsed.rows[0].debit == Decimal("5000.00")
    assert parsed.rows[0].credit is None
    assert parsed.rows[1].credit == Decimal("30000.00")
    assert parsed.rows[1].debit is None


def test_text_line_pdf_with_empty_columns_keeps_both_sides():
    # Wrapped PDF text: empty Withdrawal/Deposit cells collapse into spaces,
    # so the opening balance has to verify the very first row too.
    pdf = _make_pdf(
        [
            "HDFC BANK Account Statement",
            "A/C 1234567890123456 Opening Balance as on 01-09-2025: 20,000.00",
            "01-09-2025 UPI/KALYAN/kalyan@okaxis/12345 5000.00  15000.00",
            "05-09-2025 SALARY CREDIT FROM EMPLOYER  30000.00 45000.00",
            "10-09-2025 PAYTM-KALYAN-XXXX1234 2000.00  43000.00",
        ]
    )
    parsed = parse_statement("pdf", pdf, filename="statement.pdf")

    assert len(parsed.rows) == 3
    assert parsed.rows[0].debit == Decimal("5000.00")
    assert parsed.rows[1].credit == Decimal("30000.00")
    assert parsed.rows[2].debit == Decimal("2000.00")


def test_correct_statement_is_never_modified_by_reconciliation():
    content = """Date,Narration,Withdrawal (INR),Deposit (INR),Closing Balance
01-09-2025,UPI/KALYAN/kalyan@okaxis/12345,5000.00,,15000.00
05-09-2025,SALARY CREDIT FROM EMPLOYER,,30000.00,45000.00
10-09-2025,PAYTM-KALYAN-XXXX1234,2000.00,,43000.00
"""
    parsed = parse_statement("csv", content.encode("utf-8"), filename="x.csv")

    assert parsed.rows[0].debit == Decimal("5000.00")
    assert parsed.rows[1].credit == Decimal("30000.00")
    assert parsed.rows[2].debit == Decimal("2000.00")
    assert parsed.warnings == []


def test_encrypted_pdf_upload_flow(test_app, auth_headers):
    # TEST 10: ask for the password securely; never log/return it.
    password = "TopSecret123!"
    pdf = _make_pdf(
        [
            "01-09-2025 UPI/KALYAN/kalyan@okaxis/12345 5000.00 Dr 15000.00",
            "05-09-2025 UPI/KALYAN/kalyan@okaxis/12346 2500.00 Dr 12500.00",
        ],
        password=password,
    )

    failed = upload_and_wait(
        test_app, auth_headers, pdf, "locked.pdf", content_type="application/pdf"
    )
    assert failed["job"]["status"] == "FAILED"
    message = failed["job"].get("error") or failed["job"].get("message") or ""
    assert "password" in message.lower()
    assert password not in message

    succeeded = upload_and_wait(
        test_app,
        auth_headers,
        pdf,
        "locked.pdf",
        content_type="application/pdf",
        password=password,
        name="Encrypted Statement",
    )
    assert succeeded["job"]["status"] == "COMPLETED", succeeded["job"]
    assert succeeded["statement"]["transaction_count"] == 2
    # The password must not appear anywhere in the API responses.
    assert password not in str(succeeded)
