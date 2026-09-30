"""End-to-end: two dummy statements uploaded at once, data + AI verified.

Covers the user-facing flow:
  1. Upload two statements in ONE request (batch endpoint).
  2. Read the combined data back (statements, banks, transactions).
  3. Ask the AI anything - every answer is graded against ground truth
     recomputed from /transactions, plus anti-hallucination invariants
     (totals never exceed the dataset, unknowns get an honest refusal).
"""

from __future__ import annotations

import time
from collections import defaultdict
from decimal import Decimal

from tests.conftest import CREDIT_STATEMENT_CSV, STATEMENT_CSV

REFUSAL = (
    "I couldn't determine this confidently from the available statement data."
)


def D(value) -> Decimal:
    return Decimal(str(value)) if value not in (None, "") else Decimal("0")


def _upload_two_at_once(client, headers: dict) -> dict:
    """One request, two files - the multi-statement upload flow."""
    response = client.post(
        "/api/v1/statements/upload-batch",
        headers=headers,
        files=[
            ("files", ("hdfc-sept.csv", STATEMENT_CSV.encode("utf-8"), "text/csv")),
            (
                "files",
                ("icici-sept.csv", CREDIT_STATEMENT_CSV.encode("utf-8"), "text/csv"),
            ),
        ],
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["errors"] == [], payload["errors"]
    assert len(payload["uploaded"]) == 2

    for item in payload["uploaded"]:
        job_id = item["job"]["id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            job = client.get(
                f"/api/v1/processing/{job_id}", headers=headers
            ).json()
            if job["status"] in ("COMPLETED", "FAILED"):
                assert job["status"] == "COMPLETED", job
                break
            time.sleep(0.1)
        else:  # pragma: no cover
            raise AssertionError("Processing job did not finish in time")
    return payload


def _ground_truth(client, headers: dict) -> dict:
    items = client.get(
        "/api/v1/transactions?limit=500", headers=headers
    ).json()["items"]
    assert items, "no transactions after uploading two statements"

    person = defaultdict(lambda: {"d": Decimal(0), "n": 0, "name": ""})
    method = defaultdict(lambda: {"n": 0, "d": Decimal(0)})
    for row in items:
        if row["counterparty_code"]:
            entry = person[row["counterparty_code"]]
            entry["d"] += D(row["debit_amount"])
            entry["n"] += 1
            entry["name"] = row["counterparty_name"]
        bucket = method[row["payment_method"]]
        bucket["n"] += 1
        bucket["d"] += D(row["debit_amount"])

    debits = [row for row in items if row["debit_amount"]]
    # Same ordering as compute_closing_balance: date desc, id desc.
    last = max(items, key=lambda row: (row["transaction_date"] or "", row["id"]))
    top_person = max(person.items(), key=lambda kv: kv[1]["d"])
    return {
        "rows": items,
        "count": len(items),
        "debit": sum(D(row["debit_amount"]) for row in items),
        "credit": sum(D(row["credit_amount"]) for row in items),
        "largest": max(debits, key=lambda row: D(row["debit_amount"])),
        "last": last,
        "person": person,
        "top_person": top_person,
        "method": method,
    }


def _ask(client, headers: dict, question: str, **scope) -> dict:
    response = client.post(
        "/api/v1/rag/query",
        headers=headers,
        json={"question": question, **scope},
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------
# 1 + 2: upload two statements at once, read the combined data back
# --------------------------------------------------------------------------


def test_upload_two_statements_and_read_combined_data(test_app, auth_headers, db):
    from app.models import Statement as StatementModel

    payload = _upload_two_at_once(test_app, auth_headers)

    # Both statements exist with their own transaction counts.
    statements = test_app.get("/api/v1/statements", headers=auth_headers).json()
    assert statements["total"] == 2
    counts = sorted(item["transaction_count"] for item in statements["items"])
    assert counts == [4, 9]

    # Label the banks so the bank picker has something to show.
    rows = db.query(StatementModel).order_by(StatementModel.id).all()
    rows[0].bank_name = "HDFC Bank"
    rows[1].bank_name = "ICICI Bank"
    db.commit()

    banks = test_app.get("/api/v1/statements/banks", headers=auth_headers).json()
    assert banks["total"] == 2
    by_name = {item["bank_name"]: item for item in banks["items"]}
    assert by_name["HDFC Bank"]["statement_count"] == 1
    assert by_name["ICICI Bank"]["transaction_count"] == 4

    # Combined data across BOTH files, then split by bank / statement.
    combined = test_app.get("/api/v1/transactions", headers=auth_headers).json()
    assert combined["total"] == 13
    both = test_app.get(
        "/api/v1/transactions?statement_ids="
        f"{rows[0].id},{rows[1].id}",
        headers=auth_headers,
    ).json()
    assert both["total"] == 13
    hdfc = test_app.get(
        "/api/v1/transactions?bank=HDFC Bank", headers=auth_headers
    ).json()
    assert hdfc["total"] == 9
    icici = test_app.get(
        "/api/v1/transactions?bank=icici bank", headers=auth_headers
    ).json()
    assert icici["total"] == 4

    # Bank filter on the statements list itself.
    filtered = test_app.get(
        "/api/v1/statements?bank=HDFC Bank", headers=auth_headers
    ).json()
    assert filtered["total"] == 1


# --------------------------------------------------------------------------
# 3: the AI battery - every question graded against ground truth
# --------------------------------------------------------------------------


def test_ai_answers_any_question_against_ground_truth(test_app, auth_headers):
    _upload_two_at_once(test_app, auth_headers)
    gt = _ground_truth(test_app, auth_headers)

    # The dataset really does combine both statements.
    assert gt["count"] == 13
    assert gt["debit"] == Decimal("29750.00")  # 23250 + 6500
    assert gt["credit"] == Decimal("30250.50")  # 0 + 30250.50

    def aggregates(payload) -> dict:
        # Anti-hallucination invariants first: nothing may exceed the data.
        agg = payload.get("aggregates") or {}
        assert payload["answer"].strip(), "empty answer"
        if agg.get("total_debit") is not None:
            assert D(agg["total_debit"]) <= gt["debit"], payload
        if agg.get("total_credit") is not None:
            assert D(agg["total_credit"]) <= gt["credit"], payload
        if agg.get("count") is not None:
            assert int(agg["count"]) <= gt["count"], payload
        return agg

    # --- exact totals over BOTH statements --------------------------------
    payload = _ask(test_app, auth_headers, "What is my total credit and debit?")
    agg = aggregates(payload)
    assert payload["intent"] == "overall_totals"
    assert D(agg["total_debit"]) == gt["debit"]
    assert D(agg["total_credit"]) == gt["credit"]
    assert int(agg["count"]) == gt["count"]
    # Scope unset = answered from everything.
    assert payload["scope"]["statement_ids"] is None

    payload = _ask(test_app, auth_headers, "How much did I spend in total?")
    assert D(aggregates(payload)["total_debit"]) == gt["debit"]

    payload = _ask(test_app, auth_headers, "How much did I receive in total?")
    assert D(aggregates(payload)["total_credit"]) == gt["credit"]

    payload = _ask(test_app, auth_headers, "How many transactions in 2025?")
    assert int(aggregates(payload)["count"]) == gt["count"]

    # --- biggest / balance / ranking --------------------------------------
    payload = _ask(test_app, auth_headers, "Show my biggest transactions")
    assert D(aggregates(payload)["largest_debit"]) == D(
        gt["largest"]["debit_amount"]
    )

    payload = _ask(test_app, auth_headers, "What is my balance?")
    assert D(aggregates(payload)["closing_balance"]) == D(gt["last"]["balance"])
    assert aggregates(payload)["balance_date"] == gt["last"]["transaction_date"]

    payload = _ask(
        test_app, auth_headers, "Who did I transfer the most money to?"
    )
    tops = (aggregates(payload).get("counterparties") or {}).get("top") or []
    assert tops, payload
    assert tops[0]["total_debit"] == str(gt["top_person"][1]["d"])
    assert gt["top_person"][0] in payload["answer"]

    # --- person questions (resolved, ambiguous) ---------------------------
    payload = _ask(test_app, auth_headers, "How much did I send Kalyan Kumar?")
    assert payload["person_resolution"] == "resolved"
    codes = {item["code"] for item in payload["person_candidates"]}
    expected = sum(
        D(row["debit_amount"])
        for row in gt["rows"]
        if row["counterparty_code"] in codes
    )
    expected_count = sum(
        1 for row in gt["rows"] if row["counterparty_code"] in codes
    )
    agg = aggregates(payload)
    assert D(agg["total_debit"]) == expected
    assert int(agg["count"]) == expected_count

    payload = _ask(test_app, auth_headers, "How much did I send Ravi?")
    assert payload["person_resolution"] == "ambiguous"
    assert len(payload["person_candidates"]) >= 2
    assert not payload["aggregates"]

    # --- method filters ----------------------------------------------------
    payload = _ask(test_app, auth_headers, "How many UPI transactions did I make?")
    assert int(aggregates(payload)["count"]) == gt["method"]["UPI"]["n"]

    payload = _ask(test_app, auth_headers, "How much did I pay via NEFT?")
    assert D(aggregates(payload)["total_debit"]) == gt["method"]["NEFT"]["d"]

    # --- questions the data cannot answer: honest refusals -----------------
    for question in (
        "How much did I send to Gandhi ji?",
        "Tell me about my dog",
        "What is my passport number?",
        "Who is the president of France?",
    ):
        payload = _ask(test_app, auth_headers, question)
        assert payload["answer"].strip() == REFUSAL, (
            question,
            payload["answer"],
        )


def test_ai_combination_scope_answers_per_selection(
    test_app, auth_headers, db
):
    from app.models import Statement as StatementModel

    _upload_two_at_once(test_app, auth_headers)
    rows = db.query(StatementModel).order_by(StatementModel.id).all()
    rows[0].bank_name = "HDFC Bank"
    rows[1].bank_name = "ICICI Bank"
    db.commit()
    gt = _ground_truth(test_app, auth_headers)

    # Both statements explicitly selected = same answer as everything.
    payload = _ask(
        test_app,
        auth_headers,
        "What is my total credit and debit?",
        statement_ids=[rows[0].id, rows[1].id],
    )
    agg = payload["aggregates"]
    assert D(agg["total_debit"]) == gt["debit"]
    assert D(agg["total_credit"]) == gt["credit"]
    assert "SCOPE_SELECTED" in payload["notes"]

    # Only HDFC: 9 rows, 23250 debit, 0 credit.
    payload = _ask(
        test_app,
        auth_headers,
        "What is my total credit and debit?",
        banks=["hdfc bank"],
    )
    agg = payload["aggregates"]
    assert int(agg["count"]) == 9
    assert D(agg["total_debit"]) == Decimal("23250.00")
    assert D(agg["total_credit"]) == Decimal("0.00")
    assert payload["scope"]["banks"] == ["hdfc bank"]
    assert payload["source_label"] == "Answer based on the selected statements."

    # Only ICICI: 4 rows, 6500 debit, 30250.50 credit.
    payload = _ask(
        test_app,
        auth_headers,
        "What is my total credit and debit?",
        statement_ids=[rows[1].id],
    )
    agg = payload["aggregates"]
    assert int(agg["count"]) == 4
    assert D(agg["total_debit"]) == Decimal("6500.00")
    assert D(agg["total_credit"]) == Decimal("30250.50")

    # A bank with no data: nothing invented.
    payload = _ask(
        test_app,
        auth_headers,
        "What is my total credit and debit?",
        banks=["No Such Bank"],
    )
    assert payload["scope"]["statement_ids"] == []
    assert "SCOPE_EMPTY" in payload["notes"]
    assert payload["answer"].strip() == REFUSAL
