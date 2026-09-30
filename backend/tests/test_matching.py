"""Matching tests: spec TEST 2, 3, 4, 5, 8, 9.

Covers candidate search, evidence scoring, ambiguous handling, LOCAL_MATCHING
labelling and the JEV-enabled-but-unavailable fallback (no fake JEV).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.config import get_settings
from app.matching.service import match_transaction
from app.models.enums import MatchingMethod, MatchingStatus


def _match(db, description, **kwargs):
    from tests.conftest import make_transaction

    transaction, norm = make_transaction(db, description, **kwargs)
    outcome = match_transaction(db, get_settings(), transaction, norm)
    return transaction, outcome


def test_shared_upi_matches_existing_person(db):
    # TEST 2: KALYAN KUMAR / UPI / kalyan@okaxis matches person owning the UPI.
    from tests.conftest import seed_person

    person = seed_person(db, "Kalyan Kumar", upis=("kalyan@okaxis",))
    transaction, outcome = _match(db, "KALYAN KUMAR / UPI / kalyan@okaxis")

    assert outcome.status == MatchingStatus.MATCH
    assert outcome.person.id == person.id
    assert "UPI_EXACT_MATCH" in outcome.reason_codes
    assert transaction.matching_method == MatchingMethod.LOCAL_MATCHING  # TEST 8
    assert transaction.matching_confidence is not None  # calculated, not faked


def test_different_people_with_similar_names_stay_separate(db):
    # TEST 3: Ravi Kumar and Ravi Pavan remain separate people.
    from tests.conftest import seed_person

    kumar = seed_person(db, "Ravi Kumar", upis=("ravi@okaxis",))
    pavan = seed_person(db, "Ravi Pavan", upis=("ravipavan@ybl",))

    txn1, outcome1 = _match(db, "RAVI KUMAR / ravi@okaxis")
    txn2, outcome2 = _match(db, "RAVI PAVAN / ravipavan@ybl")

    assert outcome1.status == MatchingStatus.MATCH
    assert outcome2.status == MatchingStatus.MATCH
    assert outcome1.person.id == kumar.id
    assert outcome2.person.id == pavan.id
    assert txn1.counterparty_id != txn2.counterparty_id
    assert kumar.id != pavan.id


def test_generic_name_with_multiple_candidates_is_ambiguous(db):
    # TEST 4: transaction says only "RAVI"; Ravi Kumar and Ravi Pavan exist.
    from tests.conftest import seed_person

    seed_person(db, "Ravi Kumar", upis=("ravi@okaxis",))
    seed_person(db, "Ravi Pavan", upis=("ravipavan@ybl",))

    transaction, outcome = _match(db, "RAVI")

    assert outcome.status == MatchingStatus.AMBIGUOUS
    assert outcome.person is None
    assert outcome.needs_review is True
    assert transaction.matching_status == "AMBIGUOUS"
    assert transaction.counterparty_id is None
    assert "GENERIC_NAME_MULTIPLE_CANDIDATES" in outcome.reason_codes
    # Suggestions are offered but never auto-assigned.
    assert outcome.suggested_person is not None
    assert len(outcome.candidates) == 2


def test_masked_account_is_strong_evidence(db):
    # TEST 5: transaction contains XXXX1234 and one person has XXXX1234.
    from tests.conftest import seed_person

    person = seed_person(db, "Kalyan Kumar", accounts=("XXXX1234",))
    transaction, outcome = _match(db, "NEFT-KALYAN KUMAR-XXXX1234")

    assert outcome.status == MatchingStatus.MATCH
    assert outcome.person.id == person.id
    assert "ACCOUNT_EXACT_MATCH" in outcome.reason_codes
    assert outcome.confidence >= 0.8


def test_strong_identifier_creates_person_when_unknown(db):
    transaction, outcome = _match(db, "UPI/KALYAN/kalyan@okaxis/12345")

    assert outcome.status == MatchingStatus.MATCH
    assert outcome.person is not None
    assert outcome.person.code == "P001"
    assert outcome.person.identifier_values("UPI") == ["kalyan@okaxis"]
    assert "NEW_PERSON_STRONG_IDENTIFIER" in outcome.reason_codes
    assert transaction.counterparty_id == outcome.person.id


def test_name_only_without_candidates_is_unknown(db):
    transaction, outcome = _match(db, "SOME OBSCURE PERSON")

    assert outcome.status == MatchingStatus.UNKNOWN
    assert outcome.person is None
    assert outcome.confidence is None  # never fabricated
    assert transaction.matching_status == "UNKNOWN"


def test_repeated_transactions_accumulate_aliases(db):
    _match(db, "UPI/KALYAN/kalyan@okaxis/12345")
    transaction, outcome = _match(db, "KALYAN KUMAR / UPI / kalyan@okaxis")

    assert outcome.status == MatchingStatus.MATCH
    assert outcome.person.code == "P001"
    aliases = outcome.person.identifier_values("NAME")
    assert "Kalyan Kumar" in aliases  # display form recorded as an alias


def test_jev_disabled_uses_local_matching(db, monkeypatch):
    # TEST 8: JEV disabled -> app works, results labelled LOCAL_MATCHING.
    settings = get_settings()
    monkeypatch.setattr(settings, "jev_enabled", False)

    _, outcome = _match(db, "UPI/KALYAN/kalyan@okaxis/12345")

    assert outcome.method == MatchingMethod.LOCAL_MATCHING
    assert outcome.jev_raw is None
    assert outcome.jev_model is None


def test_jev_enabled_but_api_unreachable_forces_manual_review(db, monkeypatch):
    # TEST 9: JEV enabled but API unavailable -> no fake result; local
    # suggestion kept, transaction demoted to review with an explicit code.
    settings = get_settings()
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "dummy-key-for-test")
    monkeypatch.setattr(settings, "typesafe_api_base", "http://127.0.0.1:9")
    monkeypatch.setattr(settings, "jev_timeout_seconds", 1.0)

    transaction, outcome = _match(db, "UPI/KALYAN/kalyan@okaxis/12345")

    assert outcome.method == MatchingMethod.LOCAL_MATCHING  # never claims JEV
    assert outcome.status == MatchingStatus.AMBIGUOUS  # manual review
    assert outcome.needs_review is True
    assert outcome.person is None
    assert outcome.suggested_person is not None  # local suggestion preserved
    assert "JEV_UNAVAILABLE" in outcome.reason_codes
    assert outcome.jev_raw is None  # nothing fabricated
    assert transaction.matching_status == "AMBIGUOUS"


def test_jev_enabled_without_credentials_never_calls_out(db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "jev_enabled", True)
    monkeypatch.setattr(settings, "typesafe_api_key", "")

    _, outcome = _match(db, "UPI/KALYAN/kalyan@okaxis/12345")

    assert outcome.method == MatchingMethod.LOCAL_MATCHING
    assert "JEV_NOT_CONFIGURED" in outcome.reason_codes
    assert outcome.needs_review is True
