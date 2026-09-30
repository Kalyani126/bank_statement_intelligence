"""RAG assistant tests: spec TEST 6 and TEST 7 + no-hallucination rules."""

from __future__ import annotations

from tests.conftest import upload_standard_csv


def _ask(test_app, headers, question: str) -> dict:
    response = test_app.post(
        "/api/v1/rag/query", json={"question": question}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_total_sent_uses_sql_not_llm_arithmetic(test_app, auth_headers):
    # TEST 6: "How much did I send Kalyan Kumar?" -> exact SQL calculation.
    upload_standard_csv(test_app, auth_headers)

    data = _ask(
        test_app, auth_headers, "How much did I send Kalyan Kumar?"
    )

    assert data["person_resolution"] == "resolved"
    # P002 (Kalyan Kumar) debits: 1500 + 2000 + 900 = 4400.00 - computed by SQL.
    assert data["aggregates"]["total_debit"] == "4400.00"
    assert "4,400.00" in data["answer"]
    assert data["answer_source"] in (
        "STRUCTURED_SEARCH",
        "RAG",
        "RAG_PLUS_STRUCTURED_SEARCH",
    )
    assert data["explanation_source"] in ("TEMPLATE", "LLM")
    # Supporting rows are the exact source transactions (traceable).
    assert data["supporting_transactions"], "expected supporting rows"
    total = sum(float(t["amount"]) for t in data["supporting_transactions"] if t["type"] == "DEBIT")
    assert total == 4400.00


def test_upi_filter_with_person(test_app, auth_headers):
    # TEST 7: "Show all UPI transactions with Kalyan Kumar."
    upload_standard_csv(test_app, auth_headers)

    data = _ask(
        test_app, auth_headers, "Show all UPI transactions with Kalyan Kumar."
    )

    assert data["person_resolution"] == "resolved"
    assert data["aggregates"]["count"] >= 1
    assert data["supporting_transactions"], "expected UPI transactions"
    for item in data["supporting_transactions"]:
        assert item["payment_method"] == "UPI"
        assert item["person_code"] == "P002"


def test_count_question_returns_exact_count(test_app, auth_headers):
    upload_standard_csv(test_app, auth_headers)

    data = _ask(
        test_app, auth_headers, "How many UPI payments did I make to Kalyan?"
    )

    assert data["intent"] == "count"
    if data["person_resolution"] == "resolved":
        assert data["aggregates"]["count"] == len(
            [
                t
                for t in data["supporting_transactions"]
                if t["payment_method"] == "UPI"
            ]
        )


def test_ambiguous_persons_ask_for_review(test_app, auth_headers):
    upload_standard_csv(test_app, auth_headers)

    data = _ask(test_app, auth_headers, "How much did I send Ravi Krishna?")

    assert data["person_resolution"] == "ambiguous"
    assert "Multiple possible matches were found" in data["answer"]
    assert len(data["person_candidates"]) >= 2
    assert data["aggregates"] == {}  # no numbers invented while ambiguous


def test_unanswerable_question_is_not_invented(test_app, auth_headers):
    upload_standard_csv(test_app, auth_headers)

    data = _ask(test_app, auth_headers, "How much did I send to Gandhi ji?")

    assert data["answer"] == (
        "I couldn't determine this confidently from the available statement data."
    )
    assert data["aggregates"].get("total_debit") in (None, "0.00", 0)


def test_month_filter_and_credit_flow(test_app, auth_headers):
    upload_standard_csv(test_app, auth_headers)

    data = _ask(
        test_app, auth_headers, "Show all transactions in September 2025"
    )

    assert data["aggregates"]["count"] == 9
    assert data["aggregates"]["first_date"] == "2025-09-01"
    assert data["aggregates"]["last_date"] == "2025-09-25"


def test_rag_query_is_audited(test_app, auth_headers):
    upload_standard_csv(test_app, auth_headers)
    _ask(test_app, auth_headers, "Show my transactions with Kalyan")

    logs = test_app.get("/api/v1/system/audit-logs", headers=auth_headers).json()
    assert any(entry["action"] == "RAG_QUERY" for entry in logs)


# --------------------------------------------------------------------------
# Date understanding (year / month / person + period)
# --------------------------------------------------------------------------

def test_year_filter_is_applied_by_sql(test_app, auth_headers):
    upload_standard_csv(test_app, auth_headers)

    in_2025 = _ask(test_app, auth_headers, "How many transactions in 2025?")
    assert in_2025["aggregates"]["count"] == 9
    assert "9 transactions" in in_2025["answer"]

    in_2024 = _ask(test_app, auth_headers, "How many transactions in 2024?")
    assert in_2024["aggregates"]["count"] == 0
    assert in_2024["answer"] == (
        "I couldn't determine this confidently from the available statement data."
    )


def test_month_and_person_filter_combined(test_app, auth_headers):
    upload_standard_csv(test_app, auth_headers)

    september = _ask(
        test_app, auth_headers, "How much did I send Kalyan Kumar in September 2025?"
    )
    assert september["aggregates"]["total_debit"] == "4400.00"

    october = _ask(
        test_app, auth_headers, "How much did I send Kalyan Kumar in October 2025?"
    )
    assert october["aggregates"]["count"] == 0
    assert october["answer"] == (
        "I couldn't determine this confidently from the available statement data."
    )


def test_relative_period_words_are_not_names(test_app, auth_headers):
    upload_standard_csv(test_app, auth_headers)

    data = _ask(test_app, auth_headers, "How many transactions last month?")
    # "last month" resolves to a concrete window outside the statement period.
    assert data["intent"] == "count"
    assert data["aggregates"]["count"] == 0


# --------------------------------------------------------------------------
# Question words must never be mistaken for a counterparty
# --------------------------------------------------------------------------

def test_intent_words_are_not_treated_as_people(test_app, auth_headers):
    upload_standard_csv(test_app, auth_headers)

    biggest = _ask(test_app, auth_headers, "Show my biggest transactions")
    assert "couldn't determine" not in biggest["answer"].lower()
    assert "Largest debit" in biggest["answer"]
    assert biggest["aggregates"]["largest_debit"] == "7500.00"

    totals = _ask(test_app, auth_headers, "What is my total credit and debit?")
    assert "Total debits" in totals["answer"]
    assert totals["aggregates"]["total_debit"] == "23250.00"
    assert totals["aggregates"]["total_credit"] == "0.00"


def test_top_counterparty_question_ranks_by_sql(test_app, auth_headers):
    upload_standard_csv(test_app, auth_headers)

    data = _ask(test_app, auth_headers, "Who did I transfer the most money to?")

    breakdown = data["aggregates"]["counterparties"]
    assert breakdown["distinct"] >= 4
    assert breakdown["top"][0]["name"].startswith("Kalyan")
    assert breakdown["top"][0]["total_debit"] == "15500.00"
    assert "Kalyan" in data["answer"]
    assert "15,500.00" in data["answer"]


def test_credit_questions_are_answered_from_sql(test_app, auth_headers):
    from tests.conftest import upload_credit_csv

    upload_credit_csv(test_app, auth_headers)

    received = _ask(test_app, auth_headers, "How much did I receive in total?")
    assert received["intent"] == "total_received"
    assert received["aggregates"]["total_credit"] == "30250.50"
    assert "30,250.50" in received["answer"]

    both = _ask(test_app, auth_headers, "What is my total credit and debit?")
    assert both["aggregates"]["total_debit"] == "6500.00"
    assert both["aggregates"]["total_credit"] == "30250.50"
    assert "6,500.00" in both["answer"] and "30,250.50" in both["answer"]


def test_closing_balance_is_read_from_the_statement(test_app, auth_headers):
    upload_standard_csv(test_app, auth_headers)

    data = _ask(test_app, auth_headers, "What is my balance?")

    assert data["intent"] == "balance"
    assert data["aggregates"]["closing_balance"] == "0.00"
    assert data["aggregates"]["balance_date"] == "2025-09-25"
    assert "Closing balance" in data["answer"]


# --------------------------------------------------------------------------
# Duplicate person records (same counterparty, several rows)
# --------------------------------------------------------------------------

def test_same_name_person_records_are_merged_into_one_answer(db):
    from decimal import Decimal
    from datetime import date

    from app.config import get_settings
    from app.models.enums import PersonSource, TransactionType
    from app.models.person import Person
    from app.models.statement import Statement
    from app.models.transaction import Transaction
    from app.models.user import User
    from app.rag.service import query as rag_query

    user = db.query(User).order_by(User.id).first()
    statement = Statement(
        user_id=user.id, name="duplicate-people", file_type="CSV", status="COMPLETED"
    )
    db.add(statement)
    db.flush()

    for index, amount in enumerate((Decimal("100.00"), Decimal("200.00")), start=1):
        person = Person(
            code=f"P90{index}",
            canonical_name="Meher Mi",
            normalized_name="meher mi",
            source=PersonSource.AUTO,
        )
        db.add(person)
        db.flush()
        db.add(
            Transaction(
                statement_id=statement.id,
                transaction_date=date(2025, 9, 10),
                description="UPI/MEHER/meher@okaxis/1",
                raw_description="UPI/MEHER/meher@okaxis/1",
                narration="UPI/MEHER/meher@okaxis/1",
                raw_text="UPI/MEHER/meher@okaxis/1",
                debit_amount=amount,
                transaction_type=TransactionType.DEBIT,
                payment_method="UPI",
                counterparty_id=person.id,
                counterparty_name="Meher Mi",
                normalized_counterparty_name="meher mi",
            )
        )
    db.commit()

    result = rag_query(db, get_settings(), user, "How much did I send Meher Mi?")

    assert result["person_resolution"] == "resolved"
    assert "SAME_NAME_RECORDS_MERGED" in result["notes"]
    assert result["aggregates"]["total_debit"] == "300.00"
    assert "300.00" in result["answer"]


def test_umbrella_method_widens_when_no_exact_match(db):
    from decimal import Decimal
    from datetime import date

    from app.config import get_settings
    from app.models.enums import PersonSource, TransactionType
    from app.models.person import Person
    from app.models.statement import Statement
    from app.models.transaction import Transaction
    from app.models.user import User
    from app.rag.service import query as rag_query

    user = db.query(User).order_by(User.id).first()
    statement = Statement(
        user_id=user.id, name="paytm-only", file_type="CSV", status="COMPLETED"
    )
    db.add(statement)
    db.flush()

    person = Person(
        code="P905",
        canonical_name="Meher Mi",
        normalized_name="meher mi",
        source=PersonSource.AUTO,
    )
    db.add(person)
    db.flush()
    db.add(
        Transaction(
            statement_id=statement.id,
            transaction_date=date(2025, 9, 12),
            description="UPI/DR/000846982325/MEHER MI/YESB/paytmqr6nj",
            raw_description="UPI/DR/000846982325/MEHER MI/YESB/paytmqr6nj",
            narration="UPI/DR/000846982325/MEHER MI/YESB/paytmqr6nj",
            raw_text="UPI/DR/000846982325/MEHER MI/YESB/paytmqr6nj",
            debit_amount=Decimal("150.00"),
            transaction_type=TransactionType.DEBIT,
            payment_method="PAYTM",
            counterparty_id=person.id,
            counterparty_name="Meher Mi",
            normalized_counterparty_name="meher mi",
        )
    )
    db.commit()

    # "UPI" means the rail: a Paytm payment is still a UPI payment.
    result = rag_query(
        db, get_settings(), user, "Show all UPI transactions with Meher Mi"
    )

    assert "METHOD_FILTER_WIDENED" in result["notes"]
    assert result["aggregates"]["count"] == 1
    assert result["aggregates"]["total_debit"] == "150.00"
    assert result["supporting_transactions"]


# --------------------------------------------------------------------------
# UPI-handle-only names: person lives in identifiers, not in a stored name
# --------------------------------------------------------------------------

def _seed_upi_person(db, code: str, canonical: str, upi: str):
    from tests.conftest import seed_person

    return seed_person(db, canonical, upis=(upi,))


def test_person_resolved_by_upi_handle_when_name_is_absent(db):
    from datetime import date
    from decimal import Decimal

    from app.config import get_settings
    from app.models.enums import TransactionType
    from app.models.statement import Statement
    from app.models.transaction import Transaction
    from app.models.user import User
    from app.rag.service import query as rag_query
    from tests.conftest import make_transaction

    person = _seed_upi_person(db, "P801", "Varri Kalyan", "kalyanijay@sbin")
    # The counterparty string on the row is truncated bank text ("Varri K"),
    # which does NOT contain "kalyan" - only the UPI handle does.
    txn, _ = make_transaction(
        db,
        "UPI/DR/123/VARRI K/SBIN/kalyanijay/UPI",
        debit=Decimal("500.00"),
    )
    txn.counterparty_id = person.id
    txn.normalized_counterparty_name = "varri k"
    db.commit()

    result = rag_query(db, get_settings(), db.query(User).first(), "Total sent kalyan?")

    assert result["person_resolution"] == "resolved"
    assert result["aggregates"]["total_debit"] == "500.00"
    assert "500.00" in result["answer"]


def test_shared_upi_handle_across_duplicate_records_is_merged(db):
    from decimal import Decimal

    from app.config import get_settings
    from app.models.user import User
    from app.rag.service import query as rag_query
    from tests.conftest import make_transaction

    p1 = _seed_upi_person(db, "P811", "Varri Ka", "kalyanijay@sbin")
    p2 = _seed_upi_person(db, "P812", "Varri Kalyan", "kalyanijay@sbin")
    txn1, _ = make_transaction(db, "UPI/DR/1/VARRI K/SBIN/kalyanijay/UPI", debit=Decimal("100.00"))
    txn2, _ = make_transaction(db, "UPI/DR/2/VARRI KA/SBIN/kalyanijay/UPI", debit=Decimal("200.00"))
    txn1.counterparty_id = p1.id
    txn1.normalized_counterparty_name = "varri k"
    txn2.counterparty_id = p2.id
    txn2.normalized_counterparty_name = "varri ka"
    db.commit()

    result = rag_query(db, get_settings(), db.query(User).first(), "Total sent kalyan?")

    assert result["person_resolution"] == "resolved"
    assert "SAME_UPI_RECORDS_MERGED" in result["notes"]
    assert result["aggregates"]["total_debit"] == "300.00"


def test_different_upi_handles_stay_ambiguous(db):
    from decimal import Decimal

    from app.config import get_settings
    from app.models.user import User
    from app.rag.service import query as rag_query
    from tests.conftest import make_transaction

    p1 = _seed_upi_person(db, "P821", "Kalyan Jay", "kalyanijay@sbin")
    p2 = _seed_upi_person(db, "P822", "Kalyan Dammu", "kalyandamm@kkbk")
    txn1, _ = make_transaction(db, "UPI/DR/1/KALYAN JAY/SBIN/UPI", debit=Decimal("100.00"))
    txn2, _ = make_transaction(db, "UPI/DR/2/KALYAN DAMMU/KKBK/UPI", debit=Decimal("200.00"))
    txn1.counterparty_id = p1.id
    txn2.counterparty_id = p2.id
    db.commit()

    result = rag_query(db, get_settings(), db.query(User).first(), "Total sent kalyan?")

    assert result["person_resolution"] == "ambiguous"
    assert "MULTIPLE_PERSON_MATCHES" in result["notes"]
    assert result["aggregates"] == {}


def test_upi_local_part_hits_description_when_no_person_exists(db):
    from decimal import Decimal

    from app.config import get_settings
    from app.models.user import User
    from app.rag.service import query as rag_query
    from tests.conftest import make_transaction

    # No Person at all - the UPI handle's local part is the only trace.
    make_transaction(db, "UPI/DR/5/VARRI K/SBIN/kalyanijay/UPI", debit=Decimal("700.00"))

    result = rag_query(db, get_settings(), db.query(User).first(), "Total sent kalyan?")

    assert result["person_resolution"] != "resolved"
    assert result["aggregates"]["total_debit"] == "700.00"
    assert result["answer_source"] in (
        "STRUCTURED_SEARCH",
        "RAG_PLUS_STRUCTURED_SEARCH",
    )
    assert result["supporting_transactions"]


# --------------------------------------------------------------------------
# Upload / index plumbing
# --------------------------------------------------------------------------

def test_identical_file_upload_is_rejected(test_app, auth_headers):
    from tests.conftest import STATEMENT_CSV

    first = upload_standard_csv(test_app, auth_headers)

    response = test_app.post(
        "/api/v1/statements/upload",
        headers=auth_headers,
        files={
            "file": ("statement.csv", STATEMENT_CSV.encode("utf-8"), "text/csv")
        },
    )

    assert response.status_code == 409
    assert str(first["statement"]["id"]) in response.json()["detail"]
    statements = test_app.get("/api/v1/statements", headers=auth_headers).json()
    assert statements["total"] == 1  # nothing was double-counted


def test_reindex_endpoint_rebuilds_the_vector_index(test_app, auth_headers):
    upload_standard_csv(test_app, auth_headers)

    response = test_app.post("/api/v1/system/reindex", headers=auth_headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["documents"] == 9
    assert payload["statements"] == 1
    assert payload["embedding_provider"] == "local"
