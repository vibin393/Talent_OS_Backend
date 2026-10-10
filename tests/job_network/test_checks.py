# ruff: noqa: E501
"""Automated moderation checks (Epic 75, spec A5)."""

from app.services.job_network import checks

GOOD = "We are hiring a Senior Data Engineer to build pipelines with PySpark and Azure. " * 6


def _codes(title: str, text: str, verified: frozenset[str] = frozenset()) -> set[str]:
    return {f.code for f in checks.run(title=title, description_text=text, verified_domains=verified)}


def test_clean_listing_has_no_findings() -> None:
    assert _codes("Senior Data Engineer", GOOD) == set()


def test_fee_request_english() -> None:
    assert "fee_request" in _codes("Hiring", "Pay registration fee of Rs 500 to apply. " + GOOD)


def test_fee_request_hinglish() -> None:
    assert "fee_request" in _codes("Job", "security jama karna hoga pehle. " + GOOD)


def test_external_contact_indian_mobile() -> None:
    assert "external_contact" in _codes("Hiring", "Call +91 9812345610 to apply. " + GOOD)


def test_external_contact_email_not_on_verified_domain() -> None:
    codes = _codes("Hiring", "Mail hr@gmail.com today. " + GOOD, verified=frozenset({"acme.example.com"}))
    assert "external_contact" in codes


def test_email_on_verified_domain_is_allowed() -> None:
    codes = _codes("Hiring", "Mail careers@acme.example.com. " + GOOD, verified=frozenset({"acme.example.com"}))
    assert "external_contact" not in codes


def test_whatsapp_handle() -> None:
    assert "external_contact" in _codes("Hiring", "Ping us on wa.me/919812345610. " + GOOD)


def test_biased_terms() -> None:
    assert "biased_terms" in _codes("Hiring male only candidates", GOOD)


def test_prohibited() -> None:
    assert "prohibited" in _codes("Crypto investment agents", "Join our crypto investment scheme. " + GOOD)


def test_missing_basics_short_description() -> None:
    assert "missing_basics" in _codes("Hiring", "Too short.")


def test_redact_contacts_strips_email_and_phone() -> None:
    out = checks.redact_contacts("Reach me at foo@bar.com or 9812345610 please")
    assert "foo@bar.com" not in (out or "") and "9812345610" not in (out or "")
