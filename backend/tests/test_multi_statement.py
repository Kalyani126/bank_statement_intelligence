"""Multiple statements at once: batch upload, bank filters, combined views."""

from __future__ import annotations

import time

from tests.conftest import CREDIT_STATEMENT_CSV, STATEMENT_CSV


def _wait_for_jobs(client, headers: dict, jobs: list[dict]) -> list[dict]:
    finished = []
    for job in jobs:
        deadline = time.time() + 30
        while time.time() < deadline:
            current = client.get(
                f"/api/v1/processing/{job['id']}", headers=headers
            ).json()
            if current["status"] in ("COMPLETED", "FAILED"):
                finished.append(current)
                break
            time.sleep(0.1)
        else:  # pragma: no cover
            raise AssertionError("Processing job did not finish in time")
    return finished


def test_batch_upload_processes_every_file(test_app, auth_headers):
    files = [
        ("files", ("first.csv", STATEMENT_CSV.encode("utf-8"), "text/csv")),
        (
            "files",
            ("second.csv", CREDIT_STATEMENT_CSV.encode("utf-8"), "text/csv"),
        ),
    ]
    response = test_app.post(
        "/api/v1/statements/upload-batch", headers=auth_headers, files=files
    )
    assert response.status_code == 201, response.text
    payload = response.json()

    assert payload["errors"] == []
    assert len(payload["uploaded"]) == 2

    statuses = _wait_for_jobs(
        test_app, auth_headers, [item["job"] for item in payload["uploaded"]]
    )
    assert [job["status"] for job in statuses] == ["COMPLETED", "COMPLETED"]

    statements = test_app.get("/api/v1/statements", headers=auth_headers).json()
    assert statements["total"] == 2
    counts = sorted(item["transaction_count"] for item in statements["items"])
    assert counts == [4, 9]


def test_batch_upload_reports_errors_per_file(test_app, auth_headers):
    files = [
        ("files", ("good.csv", STATEMENT_CSV.encode("utf-8"), "text/csv")),
        ("files", ("malware.exe", b"MZ\x90\x00fake", "application/octet-stream")),
        ("files", ("empty.csv", b"", "text/csv")),
    ]
    response = test_app.post(
        "/api/v1/statements/upload-batch", headers=auth_headers, files=files
    )
    assert response.status_code == 201, response.text
    payload = response.json()

    # The good file still went through - one bad file must not sink the batch.
    assert len(payload["uploaded"]) == 1
    assert payload["uploaded"][0]["statement"]["source_file_name"] == "good.csv"
    assert len(payload["errors"]) == 2
    names = {error["filename"] for error in payload["errors"]}
    assert names == {"malware.exe", "empty.csv"}

    # Let the background job finish so it cannot race the next test's cleanup.
    _wait_for_jobs(test_app, auth_headers, [payload["uploaded"][0]["job"]])


def test_batch_upload_rejects_duplicate_within_batch(test_app, auth_headers):
    files = [
        ("files", ("same.csv", STATEMENT_CSV.encode("utf-8"), "text/csv")),
        ("files", ("same.csv", CREDIT_STATEMENT_CSV.encode("utf-8"), "text/csv")),
    ]
    response = test_app.post(
        "/api/v1/statements/upload-batch", headers=auth_headers, files=files
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert len(payload["uploaded"]) == 1
    assert len(payload["errors"]) == 1
    assert "Duplicate filename" in payload["errors"][0]["error"]
    _wait_for_jobs(test_app, auth_headers, [payload["uploaded"][0]["job"]])

    # An identical file re-uploaded in a LATER batch is refused (409-style
    # per-file error) so nothing is double-counted.
    second = test_app.post(
        "/api/v1/statements/upload-batch",
        headers=auth_headers,
        files=[
            ("files", ("same.csv", STATEMENT_CSV.encode("utf-8"), "text/csv")),
        ],
    ).json()
    assert second["uploaded"] == []
    assert "already uploaded" in second["errors"][0]["error"]

    statements = test_app.get("/api/v1/statements", headers=auth_headers).json()
    assert statements["total"] == 1


def test_batch_upload_caps_file_count(test_app, auth_headers):
    files = [
        ("files", (f"file-{index}.csv", b"a,b\n1,2\n", "text/csv"))
        for index in range(11)
    ]
    response = test_app.post(
        "/api/v1/statements/upload-batch", headers=auth_headers, files=files
    )
    assert response.status_code == 400
    assert "at most" in response.json()["detail"]


# --------------------------------------------------------------------------
# Bank + combination filters
# --------------------------------------------------------------------------


def _upload_two_with_banks(client, headers: dict) -> list[dict]:
    from tests.conftest import upload_and_wait

    first = upload_and_wait(
        client, headers, STATEMENT_CSV.encode("utf-8"), "hdfc.csv", name="HDFC"
    )
    second = upload_and_wait(
        client,
        headers,
        CREDIT_STATEMENT_CSV.encode("utf-8"),
        "icici.csv",
        name="ICICI",
    )
    return [first, second]


def test_statements_banks_endpoint_and_bank_filter(test_app, auth_headers, db):
    from app.models import Statement as StatementModel

    _upload_two_with_banks(test_app, auth_headers)

    statements = (
        db.query(StatementModel).order_by(StatementModel.id).all()
    )
    statements[0].bank_name = "HDFC Bank"
    statements[1].bank_name = "ICICI Bank"
    db.commit()

    banks = test_app.get("/api/v1/statements/banks", headers=auth_headers).json()
    assert banks["total"] == 2
    assert {item["bank_name"] for item in banks["items"]} == {
        "HDFC Bank",
        "ICICI Bank",
    }

    filtered = test_app.get(
        "/api/v1/statements?bank=hdfc bank", headers=auth_headers
    ).json()
    assert filtered["total"] == 1
    assert filtered["items"][0]["name"] == "HDFC"


def test_transactions_combined_across_statements(test_app, auth_headers, db):
    from app.models import Statement as StatementModel

    _upload_two_with_banks(test_app, auth_headers)
    statements = db.query(StatementModel).order_by(StatementModel.id).all()
    statements[0].bank_name = "HDFC Bank"
    statements[1].bank_name = "ICICI Bank"
    db.commit()
    hdfc_id, icici_id = statements[0].id, statements[1].id

    combined = test_app.get(
        f"/api/v1/transactions?statement_ids={hdfc_id},{icici_id}",
        headers=auth_headers,
    ).json()
    assert combined["total"] == 13  # 9 + 4 rows from both files

    by_bank = test_app.get(
        "/api/v1/transactions?bank=ICICI Bank", headers=auth_headers
    ).json()
    assert by_bank["total"] == 4

    single = test_app.get(
        f"/api/v1/transactions?statement_ids={hdfc_id}",
        headers=auth_headers,
    ).json()
    assert single["total"] == 9


def test_transactions_statement_ids_rejects_junk(test_app, auth_headers):
    response = test_app.get(
        "/api/v1/transactions?statement_ids=abc,2%20x,,3",
        headers=auth_headers,
    )
    assert response.status_code == 200  # junk ignored, valid ids kept
    assert "total" in response.json()


# --------------------------------------------------------------------------
# Scoped AI questions (combination answers)
# --------------------------------------------------------------------------


def _ask_scoped(test_app, headers, question: str, **scope) -> dict:
    body = {"question": question, **scope}
    response = test_app.post(
        "/api/v1/rag/query", headers=headers, json=body
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_ai_question_scoped_to_one_statement(test_app, auth_headers, db):
    from app.models import Statement as StatementModel

    _upload_two_with_banks(test_app, auth_headers)
    statements = db.query(StatementModel).order_by(StatementModel.id).all()
    statements[0].bank_name = "HDFC Bank"
    statements[1].bank_name = "ICICI Bank"
    db.commit()
    hdfc_id = statements[0].id

    # Whole dataset: both statements (13 rows).
    everything = _ask_scoped(test_app, auth_headers, "How many transactions in 2025?")
    assert everything["aggregates"]["count"] == 13
    assert everything["scope"]["statement_ids"] is None

    # Only the HDFC statement (9 rows).
    scoped = _ask_scoped(
        test_app,
        auth_headers,
        "How many transactions in 2025?",
        statement_ids=[hdfc_id],
    )
    assert scoped["aggregates"]["count"] == 9
    assert scoped["scope"]["statement_ids"] == [hdfc_id]
    assert "SCOPE_SELECTED" in scoped["notes"]
    assert scoped["source_label"] == "Answer based on the selected statements."


def test_ai_question_scoped_by_bank(test_app, auth_headers, db):
    from app.models import Statement as StatementModel

    _upload_two_with_banks(test_app, auth_headers)
    statements = db.query(StatementModel).order_by(StatementModel.id).all()
    statements[0].bank_name = "HDFC Bank"
    statements[1].bank_name = "ICICI Bank"
    db.commit()

    scoped = _ask_scoped(
        test_app, auth_headers, "What is my total credit and debit?", banks=["icici bank"]
    )
    assert scoped["scope"]["banks"] == ["icici bank"]
    # ICICI statement holds 4 rows: 6500 debit, 30250.50 credit.
    assert scoped["aggregates"]["count"] == 4
    assert scoped["aggregates"]["total_debit"] == "6500.00"
    assert scoped["aggregates"]["total_credit"] == "30250.50"


def test_ai_scope_of_unknown_bank_answers_nothing(test_app, auth_headers):
    _upload_two_with_banks(test_app, auth_headers)

    scoped = _ask_scoped(
        test_app,
        auth_headers,
        "What is my total credit and debit?",
        banks=["No Such Bank"],
    )
    assert scoped["scope"]["statement_ids"] == []
    assert "SCOPE_EMPTY" in scoped["notes"]
    assert "couldn't determine" in scoped["answer"].lower()


def test_ai_scope_never_reaches_other_users(test_app, auth_headers):
    # A foreign statement id is filtered out by ownership, never honoured.
    _upload_two_with_banks(test_app, auth_headers)

    scoped = _ask_scoped(
        test_app,
        auth_headers,
        "How many transactions in 2025?",
        statement_ids=[999999],
    )
    assert scoped["scope"]["statement_ids"] == []
    assert "SCOPE_EMPTY" in scoped["notes"]


def test_scoped_question_is_audited_with_scope(test_app, auth_headers):
    _upload_two_with_banks(test_app, auth_headers)
    statements = test_app.get("/api/v1/statements", headers=auth_headers).json()
    statement_id = statements["items"][0]["id"]

    _ask_scoped(
        test_app,
        auth_headers,
        "How many transactions in 2025?",
        statement_ids=[statement_id],
    )

    logs = test_app.get("/api/v1/system/audit-logs", headers=auth_headers).json()
    rag_logs = [log for log in logs if log["action"] == "RAG_QUERY"]
    assert rag_logs
    details = rag_logs[-1]["details"]
    assert details["scope_statement_ids"] == [statement_id]
