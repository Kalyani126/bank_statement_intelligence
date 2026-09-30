"""TEST 1 + normalization coverage for every supported payment format."""

from app.models.enums import PaymentMethod
from app.services.normalization import (
    detect_payment_method,
    normalize_description,
    normalize_upi_id,
)


def test_upi_description_extracts_name_upi_and_reference():
    # TEST 1: UPI/KALYAN/kalyan@okaxis/12345
    result = normalize_description("UPI/KALYAN/kalyan@okaxis/12345")
    assert result.payment_method == PaymentMethod.UPI
    assert result.normalized_upi_id == "kalyan@okaxis"
    assert result.reference == "12345"
    assert "KALYAN" in result.name_tokens
    assert result.raw_description == "UPI/KALYAN/kalyan@okaxis/12345"


def test_slash_separated_full_name_with_upi():
    # TEST 2 format: KALYAN KUMAR / UPI / kalyan@okaxis
    result = normalize_description("KALYAN KUMAR / UPI / kalyan@okaxis")
    assert result.payment_method == PaymentMethod.UPI
    assert result.name == "Kalyan Kumar"
    assert result.normalized_upi_id == "kalyan@okaxis"


def test_dash_routing_prefix_is_stripped_from_upi():
    result = normalize_description("UPI-KALYAN-KALYAN@OKAXIS")
    assert result.normalized_upi_id == "kalyan@okaxis"
    assert result.payment_method == PaymentMethod.UPI
    assert "KALYAN" in result.name_tokens


def test_transfer_with_masked_account():
    result = normalize_description("Transfer to Kalyan Kumar A/C XXXX1234")
    assert result.name == "Kalyan Kumar"
    assert result.account_identifier == "XXXX1234"
    assert result.payment_method == PaymentMethod.BANK_TRANSFER


def test_neft_format_extracts_all_fields():
    result = normalize_description("NEFT-KALYAN KUMAR-XXXX1234")
    assert result.payment_method == PaymentMethod.NEFT
    assert result.name == "Kalyan Kumar"
    assert result.account_identifier == "XXXX1234"
    assert result.reference is None or "XXXX" not in (result.reference or "")


def test_paytm_and_phonepe_formats():
    paytm = normalize_description("PAYTM-KALYAN-XXXX1234")
    assert paytm.payment_method == PaymentMethod.PAYTM
    assert paytm.name == "Kalyan"
    assert paytm.account_identifier == "XXXX1234"

    phonepe = normalize_description("PHONEPE-KALYAN@OKAXIS")
    assert phonepe.payment_method == PaymentMethod.PHONEPE
    assert phonepe.normalized_upi_id == "kalyan@okaxis"


def test_case_insensitive_upi_normalization():
    assert normalize_upi_id("KALYAN@OKAXIS") == normalize_upi_id("kalyan@okaxis")
    assert normalize_upi_id("...kalyan@okaxis") == "kalyan@okaxis"


def test_partial_inputs_are_accepted_not_rejected():
    for text in ("kalyan@okaxis", "XXXX1234", "KALYAN", "KALYAN K", "UPI/KALYAN/12345"):
        result = normalize_description(text)
        assert result.raw_description == text
        assert result.name or result.normalized_upi_id or result.account_identifier


def test_ravi_variants_stay_distinct():
    kumar = normalize_description("RAVI KUMAR / ravi@okaxis")
    pavan = normalize_description("RAVI PAVAN / ravipavan@ybl")
    assert kumar.normalized_upi_id == "ravi@okaxis"
    assert pavan.normalized_upi_id == "ravipavan@ybl"
    assert kumar.name != pavan.name


def test_hyphenated_real_upi_handle_is_preserved():
    result = normalize_description("UPI/ravi-kumar/ravi-kumar@okaxis/99887766")
    assert result.normalized_upi_id == "ravi-kumar@okaxis"
    assert result.reference == "99887766"
    assert result.name == "Ravi Kumar"


def test_payment_method_detection():
    assert detect_payment_method("GPAY-KALYAN") == PaymentMethod.GOOGLE_PAY
    assert detect_payment_method("IMPS transfer") == PaymentMethod.IMPS
    assert detect_payment_method("RTGS-KALYAN") == PaymentMethod.RTGS
    assert detect_payment_method("ATM WDL xyz") == PaymentMethod.ATM
    assert detect_payment_method("random narration") is None
