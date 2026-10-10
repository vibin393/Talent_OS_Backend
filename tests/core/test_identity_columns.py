"""Epic 21 A2 (NFR-24): no column may hold a full Aadhaar number -- only ``*_last4`` and ``*redaction*``."""

from app.core.models import Base


def test_no_aadhaar_columns_except_last4_and_redaction() -> None:
    import app.models  # noqa: F401  (register every model)

    offenders = [
        f"{t.name}.{c.name}"
        for t in Base.metadata.tables.values()
        for c in t.columns
        if "aadhaar" in c.name.lower() and not (c.name.endswith("_last4") or "redaction" in c.name)
    ]
    assert offenders == []
