# ruff: noqa: E501, RUF001
"""Epic 55 story 55.1 (pure): sender parsing, SPF / DKIM alignment, forwarded-header parsing, domain rules, MIME
parsing and the extraction quote check. No database."""

import pytest

from app.core.errors import ValidationFailed
from app.schemas.demand_intake import IntakeRolesExtraction
from app.services.demand_intake import matching
from app.services.demand_intake.extraction import normalise, quoted, validate
from app.services.demand_intake.receive import build_eml, parse_mime
from app.services.demand_intake.settings import is_public_domain, normalise_domain


def test_parse_address_never_uses_display_name() -> None:
    assert matching.parse_address("Acme HR <ACME.HR@Gmail.com>") == ("Acme HR", "acme.hr@gmail.com")
    assert matching.parse_address("Acme Analytics")[1] is None
    assert matching.domain_of("priya@Acme.example") == "acme.example"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, "unknown"),
        ("mx; dkim=pass header.d=acme.example; spf=fail", "pass"),
        ("mx; dkim=fail header.d=acme.example; spf=pass smtp.mailfrom=bounce.acme.example", "pass"),
        ("mx; dkim=pass header.d=evil.test; spf=pass smtp.mailfrom=evil.test", "fail"),
        ("mx; dkim=fail header.d=acme.example; spf=softfail smtp.mailfrom=acme.example", "fail"),
    ],
)
def test_auth_result_alignment(header: str | None, expected: str) -> None:
    assert matching.auth_result(header, "acme.example") == expected


def test_forwarded_original_from() -> None:
    body = "FYI\n\n---------- Forwarded message ---------\nFrom: Priya Sharma <Priya@Acme.example>\nDate: Mon\nSubject: roles"
    assert matching.forwarded_original_from(body) == "priya@acme.example"
    outlook = "See below\n\n-----Original Message-----\nFrom: Ravi <ravi@globex.example>\nSent: Monday"
    assert matching.forwarded_original_from(outlook) == "ravi@globex.example"
    assert matching.forwarded_original_from("no header here") is None


def test_domain_rules() -> None:
    assert normalise_domain("@Acme.COM") == "acme.com"
    assert normalise_domain("priya@acme.co.in") == "acme.co.in"
    assert (
        is_public_domain("gmail.com") and is_public_domain("outlook.com") and not is_public_domain("acme.com")
    )
    with pytest.raises(ValidationFailed):
        normalise_domain("not a domain")


def test_parse_mime_html_and_attachments() -> None:
    raw = build_eml(
        from_addr="Priya <priya@acme.example>", to="req-x@intake.test", subject="Roles", body="<p>Need <b>2</b> testers</p>",
        html=True, attachments=(("notes.txt", "text/plain", b"Selenium, Java"),), auth_results="mx; dkim=pass header.d=acme.example",
    )  # fmt: skip
    parsed = parse_mime(raw)
    assert parsed.from_address == "priya@acme.example" and parsed.subject == "Roles"
    assert "Need 2 testers" in parsed.body_text and "<b>" not in parsed.body_text
    assert [p.filename for p in parsed.attachments] == ["notes.txt"]
    assert parsed.auth_header and "dkim=pass" in parsed.auth_header and parsed.message_id


def test_quote_check_normalises_dashes_and_spaces() -> None:
    source = normalise("Budget 18–22 LPA,\n  Pune   hybrid")
    assert quoted("18-22 LPA", source) and quoted("Pune hybrid", source)
    assert not quoted("99 LPA", source) and not quoted("", source)


def test_validate_reuses_p1_sanitize() -> None:
    raw = IntakeRolesExtraction.model_validate(
        {
            "roles": [
                {
                    "title": {"value": "Data Engineer", "source_quote": "Data Engineer"},
                    "work_mode": {"value": "WFH", "source_quote": "WFH"},
                    "positions": {"value": 0, "source_quote": "0 positions"},
                    "country": {"value": "India", "source_quote": "India"},
                }
            ]
        }
    )
    out = validate(raw, "Data Engineer, WFH, India, 0 positions", 5)
    # P1 sanitize: WFH -> remote; positions 0 and a non-ISO country are dropped
    assert out.roles[0].fields["work_mode"].value == "remote"
    assert set(out.roles[0].fields) == {"title", "work_mode"}
