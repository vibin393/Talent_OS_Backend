"""AD-17: the one GSTIN validator (Epic 13, story 13.2)."""

import pytest

from app.services.legal_entities.gstin import check_character, validate_gstin

VALID = ["27AAPFU0939F1ZV", "29AAGCB7383J1Z4"]


def _make(state: str, pan: str, entity: str = "1") -> str:
    first14 = f"{state}{pan}{entity}Z"
    return first14 + check_character(first14)


@pytest.mark.parametrize("gstin", VALID)
def test_valid_samples(gstin: str) -> None:
    assert validate_gstin(gstin) == []
    assert validate_gstin(gstin.lower()) == []
    assert validate_gstin(gstin, state_code=gstin[:2], pan=gstin[2:12]) == []


def test_generated_gstins_are_valid() -> None:
    for state in ("01", "07", "33", "97"):
        assert validate_gstin(_make(state, "ABCDE1234F")) == []


@pytest.mark.parametrize("bad", ["", "27AAPFU0939F1Z", "27AAPFU0939F0ZV", "AAAPFU0939F1ZVX", "27AAPFU0939F1XV"])
def test_format(bad: str) -> None:
    assert [e.code for e in validate_gstin(bad)] == ["gstin_format"]


def test_checksum() -> None:
    gstin = VALID[0][:14] + ("A" if VALID[0][14] != "A" else "B")
    errors = validate_gstin(gstin)
    assert [e.code for e in errors] == ["gstin_checksum"]
    assert errors[0].field == "gstin"


def test_state_mismatch() -> None:
    assert [e.code for e in validate_gstin(VALID[0], state_code="29")] == ["gstin_state_mismatch"]


def test_pan_mismatch() -> None:
    assert [e.code for e in validate_gstin(VALID[0], pan="ABCDE1234F")] == ["gstin_pan_mismatch"]


def test_multiple_errors_reported() -> None:
    gstin = VALID[0][:14] + ("A" if VALID[0][14] != "A" else "B")
    codes = [e.code for e in validate_gstin(gstin, state_code="29", pan="ABCDE1234F")]
    assert codes == ["gstin_checksum", "gstin_state_mismatch", "gstin_pan_mismatch"]
