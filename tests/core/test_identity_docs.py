"""Epic 21 A2 (NFR-24): Aadhaar detection (Verhoeff), validators and the log mask."""

import pytest
from loguru import logger

from app.core.errors import ValidationFailed
from app.core.identity_docs import (
    PAN_RE,
    PASSPORT_RE,
    assert_no_full_aadhaar,
    looks_like_full_aadhaar,
    mask_full_aadhaar,
    mask_last4,
    validate_aadhaar_last4,
    verhoeff_check_digit,
    verhoeff_valid,
)
from app.core.logger import redact

VALID = "234567890124"  # Verhoeff-valid, starts with 2


def test_verhoeff_samples() -> None:
    assert verhoeff_valid(VALID)
    assert not verhoeff_valid("234567890123")
    assert verhoeff_check_digit("23456789012") == "4"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2345 6789 0124", True),
        ("2345-6789-0124", True),
        ("234567890124", True),
        ("note: id 2345 6789 0124 given", True),
        ("2345 6789 0123", False),  # checksum fails
        ("1345 6789 0124", False),  # starts with 1
        ("0345 6789 0124", False),  # starts with 0
        ("12345678901245", False),  # embedded in a longer number
        ("phone 98450 12345", False),
        ("", False),
        (None, False),
    ],
)
def test_detection(text: str | None, expected: bool) -> None:
    assert looks_like_full_aadhaar(text) is expected


def test_assert_and_validators() -> None:
    with pytest.raises(ValidationFailed) as exc:
        assert_no_full_aadhaar("fine", "2345 6789 0124")
    assert exc.value.code == "aadhaar_full_number_forbidden"
    assert_no_full_aadhaar("fine", None, 12)
    assert validate_aadhaar_last4(" 0124 ") == "0124"
    for bad in ("124", "01245", "abcd", None):
        with pytest.raises(ValidationFailed):
            validate_aadhaar_last4(bad)
    assert PASSPORT_RE.match("K1234567") and not PASSPORT_RE.match("K123456")
    assert PAN_RE.match("ABCDE1234F") and not PAN_RE.match("ABCDE12345")
    assert mask_last4("K1234567") == "XXXX4567"


def test_mask_and_log_filter() -> None:
    assert mask_full_aadhaar("id 2345 6789 0124 ok") == "id XXXX-XXXX-0124 ok"
    assert redact("aadhaar=234567890124") == "aadhaar=XXXX-XXXX-0124"
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), format="{message}")
    try:
        logger.info("Worker typed 2345-6789-0124 in a note")
    finally:
        logger.remove(sink)
    assert lines and "2345-6789-0124" not in lines[0] and "XXXX-XXXX-0124" in lines[0]
