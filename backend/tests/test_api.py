"""API-level tests: auth, upload e2e, search, review, dashboard, security."""

from __future__ import annotations

import pytest

from tests.conftest import STATEMENT_CSV, upload_and_wait, upload_standard_csv

EXPECTED_TOTAL_DEBIT = "23250.00"


# --------------------------------------------------------------------------
# Auth / security
# --------------------------------------------------------------------------

def test_endpoints_require_authentication(test_app):
    assert test_app.get("/api/v1/statements").status_code == 401
    assert test_app.get("/api/v1/dashboard/summary").status_code == 401
    assert test_app.get("/api/v1/people").status_code == 401
    bogus = {"Authorization": "Bearer not-a-real-token"}
    assert test_app.get("/api/v1/statements", headers=bogus).status_code == 401


def test_login_rejects_wrong_password(test_app):
    response = test_app.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "wrong-password"},
    )
    assert response.status_code == 401
    assert "token" not in response.text.lower()


def test_unsupported_file_extension_rejected(test_app, auth_headers):
    response = test_app.post(
        "/api/v1/statements/upload",
        headers=auth_headers,
        files={"file": ("malware.exe", b"MZ\x90\x00fake", "application/octet-stream")},
    )
    assert response.status_code == 400
    assert "Unsupported file type" in response.json()["detail"]


def test_extension_and_content_mismatch_rejected(test_app, auth_headers):
    response = test_app.post(
        "/api/v1/statements/upload",
        headers=auth_headers,
        files={"file": ("statement.pdf", b"Date,Narration\nnot a pdf", "application/pdf")},
    )
    assert response.status_code == 400


def test_upload_size_limit_enforced(test_app, auth_headers, settings_obj, monkeypatch):
    monkeypatch.setattr(settings_obj, "max_upload_size_mb", 0)
    response = test_app.post(
        "/api/v1/statements/upload",
        headers=auth_headers,
        files={"file": ("statement.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert response.status_code == 413


def test_system_status_exposes_no_secrets(test_app, auth_headers):
    response = test_app.get("/api/v1/system/status", headers=auth_headers)
    assert response.status_code == 200
    payload = response.json()
    assert "typesafe_api_key" not in payload
    assert "openai_api_key" not in payload
    assert "secret_key" not in payload
    text = response.text
    assert "BankApp" not in text and "sk-" not in text
    assert payload["jev_enabled"] is False
    assert payload["rag_enabled"] is True
    assert ".pdf" in payload["allowed_extensions"]


# --------------------------------------------------------------------------
# Upload + processing pipeline
# --------------------------------------------------------------------------

def test_upload_and_process_statement(test_app, auth_headers):
    payload = upload_standard_csv(test_app, auth_headers)

    assert payload["job"]["status"] == "COMPLETED", payload["job"]
    statement = payload["statement"]
    assert statement["status"] == "COMPLETED"
    assert statement["transaction_count"] == 9
    assert statement["period_start"] == "2025-09-01"
    assert statement["period_end"] == "2025-09-25"

    txns = test_app.get(
        f"/api/v1/statements/{statement['id']}/transactions",
        headers=auth_headers,
    ).json()
    assert txns["total"] == 9

    row = next(
        item
        for item in txns["items"]
        if "kalyan@okaxis/12345" in (item["raw_description"] or "")
    )
    # Raw preserved, normalized stored separately.
    assert row["raw_description"] == "UPI/KALYAN/kalyan@okaxis/12345"
    assert row["normalized_upi_id"] == "kalyan@okaxis"
    assert row["payment_method"] == "UPI"
    assert row["transaction_type"] == "DEBIT"
    # JEV disabled -> LOCAL_MATCHING, clearly labelled, never "JEV".
    assert row["matching_method"] == "LOCAL_MATCHING"
    assert row["matching_status"] == "MATCH"
    assert row["counterparty_code"] == "P001"
    assert row["matching_confidence"] is not None


def test_statement_filters(test_app, auth_headers):
    upload_standard_csv(test_app, auth_headers)
    base = "/api/v1/transactions"

    upi = test_app.get(f"{base}?method=UPI", headers=auth_headers).json()
    assert upi["total"] >= 3
    assert all(item["payment_method"] == "UPI" for item in upi["items"])

    debits = test_app.get(f"{base}?type=DEBIT", headers=auth_headers).json()
    assert debits["total"] == 9

    ambiguous = test_app.get(f"{base}?match_status=AMBIGUOUS", headers=auth_headers).json()
    assert ambiguous["total"] == 1
    assert ambiguous["items"][0]["raw_description"] == "RAVI"


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------

def test_search_supports_every_required_form(test_app, auth_headers):
    upload_standard_csv(test_app, auth_headers)
    base = "/api/v1/transactions"

    cases = {
        "kalyan": 4,               # partial/full name
        "Kalyan Kumar": 3,         # full name
        "kalyan@okaxis": 3,        # exact UPI id
        "@okaxis": 3,              # partial UPI id
        "XXXX1234": 3,             # masked account (via person identifiers)
        "UPI/KALYAN": 3,           # description form
        "12345": 1,                # reference
        "ravipavan@ybl": 1,        # exact UPI of the other person
    }
    for query, minimum in cases.items():
        response = test_app.get(
            f"{base}",
            params={"q": query},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["total"] >= minimum, f"search {query!r} too narrow"


# --------------------------------------------------------------------------
# Manual review
# --------------------------------------------------------------------------

def test_ambiguous_list_confirm_and_reject(test_app, auth_headers):
    upload_standard_csv(test_app, auth_headers)

    ambiguous = test_app.get("/api/v1/matches/ambiguous", headers=auth_headers).json()
    assert ambiguous["total"] == 1
    item = ambiguous["items"][0]
    assert item["transaction"]["raw_description"] == "RAVI"
    assert item["match"]["status"] == "AMBIGUOUS"
    assert item["match"]["needs_review"] is True
    # Evidence is shown for the reviewer (candidates + reason codes).
    assert item["match"]["candidates"], "expected candidate evidence"
    assert item["match"]["suggested_person_id"] in ("P003", "P004")

    transaction_id = item["transaction"]["id"]

    # Confirm the suggested person (TEST: manual decisions are saved).
    confirmed = test_app.post(
        f"/api/v1/matches/{transaction_id}/confirm",
        headers=auth_headers,
        json={"person_code": item["match"]["suggested_person_id"], "note": "verified"},
    )
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["matching_status"] == "MATCH"
    assert body["matching_method"] == "MANUAL"
    assert body["matching_confidence"] is None  # no fabricated confidence
    assert body["counterparty_code"] == item["match"]["suggested_person_id"]
    assert body["match"]["reviewed_by"] == "admin"
    assert body["match"]["needs_review"] is False

    after = test_app.get("/api/v1/matches/ambiguous", headers=auth_headers).json()
    assert after["total"] == 0

    # A fresh ambiguous row gets rejected (mark unknown). Re-uploading the
    # identical file is refused (409), so a byte-different statement is used.
    upload_and_wait(
        test_app,
        auth_headers,
        STATEMENT_CSV.replace("UPIREF001", "UPIREF002").encode("utf-8"),
        "statement.csv",
        name="September Statement (second export)",
    )
    ambiguous = test_app.get("/api/v1/matches/ambiguous", headers=auth_headers).json()
    assert ambiguous["total"] == 1
    rejected = test_app.post(
        f"/api/v1/matches/{ambiguous['items'][0]['transaction']['id']}/reject",
        headers=auth_headers,
        json={"reason": "not a real counterparty"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["matching_status"] == "UNKNOWN"
    assert rejected.json()["matching_method"] == "MANUAL"
    assert rejected.json()["match"]["needs_review"] is False

    # Audit trail exists for both manual decisions.
    logs = test_app.get("/api/v1/system/audit-logs", headers=auth_headers).json()
    actions = {entry["action"] for entry in logs}
    assert "MANUAL_MATCH_CONFIRM" in actions
    assert "MANUAL_MATCH_REJECT" in actions


# --------------------------------------------------------------------------
# People / dashboard
# --------------------------------------------------------------------------

def test_people_endpoints(test_app, auth_headers):
    upload_standard_csv(test_app, auth_headers)

    people = test_app.get("/api/v1/people", headers=auth_headers).json()
    assert people["total"] == 4  # Kalyan, Kalyan Kumar, Ravi Kumar, Ravi Pavan

    kalyan = test_app.get("/api/v1/people/P001", headers=auth_headers).json()
    assert kalyan["canonical_name"] == "Kalyan"
    assert "kalyan@okaxis" in kalyan["upi_ids"]
    assert "Kalyan Kumar" in kalyan["aliases"]  # alias accumulated from match
    assert kalyan["summary"]["total_sent"] == "15500.00"
    assert kalyan["summary"]["transaction_count"] == 3
    assert kalyan["transactions"], "expected matched transactions"
    assert kalyan["recent_matches"], "expected matching evidence"

    txns = test_app.get("/api/v1/people/P001/transactions", headers=auth_headers).json()
    assert txns["total"] == 3

    kumar = test_app.get("/api/v1/people/P002", headers=auth_headers).json()
    assert kumar["account_identifiers"] == ["XXXX1234"]
    assert kumar["summary"]["total_sent"] == "4400.00"


def test_dashboard_summary(test_app, auth_headers):
    upload_standard_csv(test_app, auth_headers)

    payload = test_app.get("/api/v1/dashboard/summary", headers=auth_headers).json()
    totals = payload["totals"]
    assert totals["statements"] == 1
    assert totals["transactions"] == 9
    assert totals["total_debit"] == EXPECTED_TOTAL_DEBIT
    assert totals["total_credit"] == "0.00"
    assert totals["unique_counterparties"] == 4
    assert totals["pending_reviews"] == 1

    charts = payload["charts"]
    assert charts["monthly"] and charts["monthly"][0]["period"] == "2025-09"
    assert charts["payment_methods"]
    assert len(charts["activity"]) == 30


def test_credit_rows_are_stored_and_summed(test_app, auth_headers):
    # A statement with real deposits: credits must reach the DB and totals.
    from tests.conftest import upload_credit_csv

    payload = upload_credit_csv(test_app, auth_headers)
    assert payload["job"]["status"] == "COMPLETED", payload["job"]
    assert payload["statement"]["transaction_count"] == 4

    txns = test_app.get(
        f"/api/v1/statements/{payload['statement']['id']}/transactions",
        headers=auth_headers,
    ).json()
    credits = [t for t in txns["items"] if t["transaction_type"] == "CREDIT"]
    debits = [t for t in txns["items"] if t["transaction_type"] == "DEBIT"]
    assert len(debits) == 2, "withdrawals must stay debits"
    assert len(credits) == 2, "deposits must be stored as credits"

    totals = test_app.get("/api/v1/dashboard/summary", headers=auth_headers).json()[
        "totals"
    ]
    assert totals["total_debit"] == "6500.00"
    assert totals["total_credit"] == "30250.50"


def test_processing_job_endpoint_ownership(test_app, auth_headers):
    payload = upload_standard_csv(test_app, auth_headers)
    job_id = payload["job"]["id"]

    job = test_app.get(f"/api/v1/processing/{job_id}", headers=auth_headers)
    assert job.status_code == 200
    assert job.json()["status"] == "COMPLETED"

    missing = test_app.get("/api/v1/processing/999999", headers=auth_headers)
    assert missing.status_code == 404


# --------------------------------------------------------------------------
# JEV adapter contract (real TypeSafe request/response shape)
# --------------------------------------------------------------------------

def _typesafe_response() -> dict:
    """Documented TypeSafe System One response shape (docs.litellm.ai/typesafe)."""
    return {
        "model": "jev-1.13.0",
        "answers": {
            "match_decision": {
                "type": "choice",
                "choice": "MATCH",
                "probabilities": {"MATCH": 0.9, "AMBIGUOUS": 0.07, "UNKNOWN": 0.03},
                "confidence": 0.9,
            },
            "matched_person": {
                "type": "choice",
                "choice": "P001",
                "probabilities": {"P001": 0.92, "none": 0.08},
                "confidence": 0.92,
            },
            "primary_reason": {
                "type": "choice",
                "choice": "UPI_EXACT_MATCH",
                "probabilities": {"UPI_EXACT_MATCH": 1.0},
                "confidence": 1.0,
            },
        },
        "usage": {"input_tokens": 210, "output_tokens": 34},
    }


def test_jev_adapter_sends_real_typesafe_request_and_parses_response(monkeypatch):
    import app.jev.client as client_module
    from app.jev.client import TypeSafeJEVClient
    from app.jev.matcher import parse_decision

    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return _typesafe_response()

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr(client_module.httpx, "post", fake_post)

    client = TypeSafeJEVClient(
        api_key="test-typesafe-key",
        base_url="https://api.typesafe.ai",
        model="jev-latest",
    )
    raw = client.evaluate(
        state='{"transaction":{"raw_description":"x"}}',
        questions={
            "match_decision": {
                "type": "choice",
                "instructions": "decide",
                "criteria": {"MATCH": "a", "AMBIGUOUS": "b", "UNKNOWN": "c"},
            }
        },
    )

    # Real endpoint contract.
    assert captured["url"] == "https://api.typesafe.ai/v1/systemone"
    assert captured["headers"]["Authorization"] == "Bearer test-typesafe-key"
    assert captured["json"]["model"] == "jev-latest"
    assert captured["json"]["state"].startswith("{")

    decision = parse_decision(raw, {"P001"})
    assert decision.status == "MATCH"
    assert decision.person_id == "P001"
    assert decision.confidence == 0.9  # provided by TypeSafe, not fabricated
    assert decision.model == "jev-1.13.0"
    assert decision.reason_codes == ["UPI_EXACT_MATCH"]


def test_jev_enabled_and_reachable_records_method_jev(db, monkeypatch):
    import app.jev.client as client_module
    from app.config import get_settings
    from app.matching.service import match_transaction
    from app.models.enums import MatchingMethod, MatchingStatus
    from tests.conftest import make_transaction

    settings = get_settings()
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    monkeypatch.setattr(settings, "typesafe_api_base", "https://api.typesafe.ai")

    class FakeResponse:
        status_code = 200

        def json(self):
            return _typesafe_response()

    monkeypatch.setattr(
        client_module.httpx, "post", lambda *args, **kwargs: FakeResponse()
    )

    transaction, norm = make_transaction(db, "UPI/KALYAN/kalyan@okaxis/12345")
    outcome = match_transaction(db, settings, transaction, norm)

    assert outcome.status == MatchingStatus.MATCH
    assert outcome.method == MatchingMethod.JEV  # only recorded when JEV answered
    assert outcome.person is not None
    assert outcome.person.code == "P001"
    assert outcome.confidence == 0.9
    assert outcome.jev_model == "jev-1.13.0"
    assert outcome.jev_raw is not None
    assert "UPI_EXACT_MATCH" in outcome.reason_codes


def test_rag_disabled_still_answers_structurally(db, monkeypatch):
    # RAG_ENABLED=false: structured search still works and never invents data.
    from app.config import get_settings
    from app.models.user import User
    from app.rag.service import query as rag_query

    settings = get_settings()
    monkeypatch.setattr(settings, "rag_enabled", False)

    user = db.query(User).order_by(User.id).first()
    result = rag_query(db, settings, user, "How much did I send Kalyan Kumar?")
    assert result["answer_source"] in ("STRUCTURED_SEARCH", "RAG")
    assert (
        "couldn't determine" in result["answer"].lower()
        or "Total sent" in result["answer"]
    )
    assert "typesafe" not in result["answer"].lower()
