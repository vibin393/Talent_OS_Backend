"""Epic 36 story 36.5 (spec 0.8): Phase 3 connector categories ``assessment``, ``dsc``, ``tds_statement`` and
``fx``. Each fake works through its category Protocol and the generic ``ConnectorPort``; the manual adapters
behave as the contract says; the DB CHECK accepts the new categories."""

import hashlib
import hmac
import json
import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from app.core.database import tenant_session
from app.models.connectors import ProviderConnectorConfig
from app.services.connectors import registry
from app.services.connectors.assessment.fake import FakeAssessmentProvider
from app.services.connectors.assessment.port import AssessmentProvider, ProviderInviteDTO
from app.services.connectors.base import ConnectorConfig, ConnectorPort, ManualActionRequired
from app.services.connectors.dsc.fake import MARKER, FakeDscSigner
from app.services.connectors.dsc.port import DscSigner, DscSignRequestDTO
from app.services.connectors.fx.fake import FakeFxRateSource
from app.services.connectors.fx.port import FxRateSource
from app.services.connectors.tds_statement.fake import FakeTdsStatementSource
from app.services.connectors.tds_statement.port import TdsStatementSource
from tests.factories import create_tenant_row

SECRET = "whsec-p3"
PORTS: dict[str, type] = {
    "assessment": AssessmentProvider,
    "dsc": DscSigner,
    "tds_statement": TdsStatementSource,
    "fx": FxRateSource,
}
FAKES = (FakeAssessmentProvider, FakeDscSigner, FakeTdsStatementSource, FakeFxRateSource)


@pytest.fixture(autouse=True)
def _reset_fakes() -> Iterator[None]:
    for fake in FAKES:
        fake.reset()
    yield
    for fake in FAKES:
        fake.reset()


def _config(category: str, provider_key: str = "fake") -> ConnectorConfig:
    return ConnectorConfig(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        category=category,
        provider_key=provider_key,
        legal_entity_id=None,
        settings={},
        credentials=None,
        webhook_secret=SECRET,
    )


def _adapter(category: str, provider_key: str) -> Any:
    factory = registry.adapter_factory(category, provider_key)
    assert factory is not None, (category, provider_key)
    return factory(_config(category, provider_key))


@pytest.mark.parametrize("category", sorted(PORTS))
async def test_adapters_registered_and_implement_both_ports(category: str) -> None:
    assert {"fake", "manual"} <= set(registry.provider_keys(category))
    for provider_key in ("fake", "manual"):
        adapter = _adapter(category, provider_key)
        assert isinstance(adapter, ConnectorPort)
        assert isinstance(adapter, PORTS[category])
        assert (adapter.category, adapter.provider_key) == (category, provider_key)


@pytest.mark.parametrize("category", sorted(PORTS))
async def test_fake_generic_port_records_fails_and_verifies(category: str) -> None:
    adapter = _adapter(category, "fake")
    fake = type(adapter)
    out = await adapter.handle("sync", {"a": 1})
    assert out["ok"] is True and fake.calls == [("sync", {"a": 1})]
    fake.fail_ops.add("sync")
    with pytest.raises(RuntimeError):
        await adapter.handle("sync", {})
    body = json.dumps({"external_ref": "x"}).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert adapter.verify_webhook({"X-Fake-Signature": sig}, body) is True
    assert adapter.verify_webhook({"X-Fake-Signature": "bad"}, body) is False
    assert adapter.parse_webhook({}, body) == {"external_ref": "x"}
    fake.reset()
    assert fake.calls == [] and fake.fail_ops == set()


@pytest.mark.parametrize("category", sorted(PORTS))
async def test_manual_generic_port(category: str) -> None:
    adapter = _adapter(category, "manual")
    with pytest.raises(ManualActionRequired):
        await adapter.handle("sync", {})
    assert adapter.verify_webhook({}, b"{}") is False
    assert adapter.parse_webhook({}, b"{}") == {}


async def test_assessment_fake_and_manual() -> None:
    fake: AssessmentProvider = _adapter("assessment", "fake")
    tests = await fake.list_tests()
    assert [t.external_id for t in tests] == ["fake-python-101", "fake-sql-201"]
    inv = ProviderInviteDTO(
        tenant_id=uuid.uuid4(),
        invitation_id=uuid.uuid4(),
        external_test_id="fake-python-101",
        candidate_email="c@example.com",
        candidate_name="C",
        deadline_at=None,
        callback_url=None,
    )
    res = await fake.invite(inv)
    assert res.take_url and res.external_ref.startswith("fake-asmt:fake-python-101:")
    result = await fake.fetch_result(res.external_ref)
    assert result is not None and result.status == "completed" and result.score == Decimal("82.50")
    assert result.skill_scores == {"python": Decimal("82.50")}
    assert await fake.fetch_result("unknown") is None
    hook = await fake.handle_webhook({}, json.dumps({"external_ref": res.external_ref}).encode())
    assert hook.external_ref == res.external_ref and hook.result == result

    manual: AssessmentProvider = _adapter("assessment", "manual")
    assert list(await manual.list_tests()) == []
    with pytest.raises(ManualActionRequired):
        await manual.invite(inv)
    assert await manual.fetch_result(res.external_ref) is None


async def test_dsc_fake_and_manual() -> None:
    pdf = b"%PDF-1.7 test"
    req = DscSignRequestDTO(
        tenant_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        pdf=pdf,
        sha256=hashlib.sha256(pdf).hexdigest(),
        signer_ref="signer-1",
        reason="Invoice",
        location="Chennai",
    )
    signed = await _adapter("dsc", "fake").sign_pdf(req)
    assert signed.status == "signed" and signed.signed_pdf == pdf + MARKER and signed.external_ref
    with pytest.raises(ManualActionRequired, match="externally signed PDF"):
        await _adapter("dsc", "manual").sign_pdf(req)


async def test_tds_statement_fake_is_deterministic_and_manual_asks() -> None:
    fake: TdsStatementSource = _adapter("tds_statement", "fake")
    one = await fake.fetch("CHEA12345B", "2026-27", "Q2")
    two = await fake.fetch("CHEA12345B", "2026-27", "Q2")
    assert one == two and 1 <= len(one.lines) <= 3
    assert (one.tan, one.fy, one.quarter) == ("CHEA12345B", "2026-27", "Q2")
    line = one.lines[0]
    assert line.tds_deducted == (line.amount_paid * Decimal("0.10")).quantize(Decimal("0.01"))
    assert line.transaction_date.month == 7
    with pytest.raises(ManualActionRequired):
        await _adapter("tds_statement", "manual").fetch("CHEA12345B", "2026-27", "Q2")


async def test_fx_fake_is_deterministic_and_manual_is_empty() -> None:
    on = date(2026, 10, 6)
    pairs = [("USD", "INR"), ("INR", "USD"), ("INR", "INR")]
    rates = await _adapter("fx", "fake").fetch_rates(on, pairs)
    assert [(r.from_ccy, r.to_ccy) for r in rates] == pairs
    assert all(r.source == "feed:fake" and r.rate_date == on for r in rates)
    assert all(r.rate.as_tuple().exponent == -6 for r in rates)
    assert rates[2].rate == Decimal("1.000000")
    assert rates == list(await _adapter("fx", "fake").fetch_rates(on, pairs))
    assert list(await _adapter("fx", "manual").fetch_rates(on, pairs)) == []


@pytest.mark.parametrize(("category", "ok"), [("fx", True), ("assessment", True), ("foo", False)])
async def test_config_category_check(category: str, ok: bool) -> None:
    tenant_id = await create_tenant_row()

    async def insert() -> None:
        async with tenant_session(tenant_id) as db:
            db.add(
                ProviderConnectorConfig(
                    tenant_id=tenant_id, category=category, provider_key="manual", display_name="P3"
                )
            )
            await db.flush()

    if ok:
        await insert()
    else:
        with pytest.raises(DBAPIError):
            await insert()
