# ruff: noqa: E501
"""47.1: every P1/P2 report source and field is declared; posted facts only for finance sources."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import CATALOG, Principal, Scope
from app.core.semantic import FieldDef, ScopeSpec, SemanticContext, SemanticRegistryError, compile, execute, get_source, register_semantic_source, sources, validate
from app.core.semantic.types import PII_CLASSES
from app.models.placements import Placement
from app.services.reporting.builder.sources_p1p2 import FINANCE_SOURCES, SOURCE_KEYS
from tests.reporting.helpers import ROrg
from tests.reporting.p2 import seed as S

# Snapshot of the sources registered here (Epic 52 extends its coverage check from this list).
EXPECTED_SOURCES = (
    "demands.demand",
    "pipeline.application",
    "pipeline.submission",
    "interviews.interview",
    "offers.offer",
    "placements.placement",
    "time.timesheet",
    "billing.invoice",
    "billing.credit_note",
    "receivables.payment",
)
# Free text and identity columns are never registered.
FORBIDDEN_FIELD_WORDS = ("note", "feedback", "email_body", "jd_text", "pan", "aadhaar", "bank", "ifsc", "account_number", "comment")
# Field names carrying money that must be ``compensation`` outside finance facts.
COMPENSATION_WORDS = ("bill_rate", "pay_rate", "margin", "ctc")


def test_source_snapshot() -> None:
    assert SOURCE_KEYS == EXPECTED_SOURCES
    assert set(EXPECTED_SOURCES) <= set(sources())


@pytest.mark.parametrize("key", EXPECTED_SOURCES)
def test_every_field_is_declared(key: str) -> None:
    src = get_source(key)
    assert src.scope.permission in CATALOG
    assert src.fields, key
    for name, fd in src.fields.items():
        assert fd.pii_class in PII_CLASSES and fd.pii_class != "protected", (key, name)
        assert fd.permission is None or fd.permission in CATALOG, (key, name)
        assert fd.label, (key, name)
        tokens = set(name.replace(".", "_").split("_"))
        if fd.type == "string":  # free text / identity numbers are never registered
            assert not any(w in name for w in FORBIDDEN_FIELD_WORDS if "_" in w) and not tokens & set(FORBIDDEN_FIELD_WORDS), (key, name)
        if key not in FINANCE_SOURCES and fd.type == "decimal" and any(w in name for w in COMPENSATION_WORDS):
            assert fd.pii_class == "compensation", (key, name)
        if key in FINANCE_SOURCES:
            assert fd.pii_class in {"none", "personal"}, (key, name)


def test_registering_a_protected_field_raises() -> None:
    with pytest.raises(SemanticRegistryError):
        register_semantic_source(
            "testreports.protected",
            Placement,
            {"x": FieldDef("string", "X", "protected", lambda p: p.role_title, frozenset({"eq"}))},  # type: ignore[arg-type]
            {},
            ScopeSpec(permission="placement:read", owner_cols=(lambda p: p.recruiter_user_id,)),
            label="Bad",
            subject=lambda p: p.id,
        )
    assert "testreports.protected" not in sources()


def test_finance_sources_have_posted_fact_filters() -> None:
    for key in ("billing.invoice", "billing.credit_note", "receivables.payment", "time.timesheet"):
        assert get_source(key).base_filter is not None, key


def _finance_principal(org: ROrg) -> Principal:
    return Principal(
        type="user", id=org.admin.user_id, tenant_id=org.tenant_id,
        permissions={"billing:read": Scope.TENANT, "receivables:read": Scope.TENANT},
    )  # fmt: skip


async def test_draft_invoice_never_appears(rorg: ROrg) -> None:
    t = rorg.tenant_id
    async with tenant_session(t) as db:
        le = await S.legal_entity(db, t)
        client = await S.client(db, t, rorg.admin.user_id)
        issued, _ = await S.invoice(db, t, le, client, date(2026, 5, 2), [(Decimal("1000"), Decimal("0"))])
        draft, _ = await S.invoice(db, t, le, client, date(2026, 5, 3), [(Decimal("777"), Decimal("0"))], status="draft")
        cancelled, _ = await S.invoice(db, t, le, client, date(2026, 5, 4), [(Decimal("555"), Decimal("0"))], status="cancelled", cancel_basis="irn_cancelled")
        pay_ok = await S.payment(db, t, le, client, date(2026, 5, 5), Decimal("100"))
        pay_rev = await S.payment(db, t, le, client, date(2026, 5, 6), Decimal("90"))
        await db.execute(text("UPDATE payments SET reversed_at = now(), reversal_reason = 'bounced', reversal_date = :d WHERE id = :id"), {"d": date(2026, 5, 7), "id": pay_rev})
    async with tenant_session(t) as db:
        ctx = SemanticContext(RequestContext(tenant_id=t, principal=_finance_principal(rorg)), db, "report")
        inv = await execute(await compile(validate({"source": "billing.invoice", "fields": ["id", "taxable_total"]}), ctx), ctx)
        assert {r["id"] for r in inv.rows} == {issued}
        assert draft not in {r["id"] for r in inv.rows} and cancelled not in {r["id"] for r in inv.rows}
        pays = await execute(await compile(validate({"source": "receivables.payment", "fields": ["id"]}), ctx), ctx)
        assert {r["id"] for r in pays.rows} == {pay_ok}
