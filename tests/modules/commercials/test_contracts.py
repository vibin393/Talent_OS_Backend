"""Contracts (FR-89, §5.3): transition table, activation guards, approvals, amendments, beat jobs, signature
completion, SOW milestones + posting source (Epic 16 A3-A4)."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.actors import ActorRef
from app.core.database import tenant_session
from app.core.errors import ConflictError, Forbidden
from app.core.events import subscribe, unsubscribe
from app.events.commercials import ContractRenewalDue, ContractStatusChanged
from app.models.approvals import ApprovalChain, ApprovalChainStep
from app.models.commercials import Contract
from app.schemas.commercials import ContractDraftIn
from app.services import commercials_service
from app.services.commercials import contracts
from tests.modules.commercials.helpers import (
    API,
    PDF,
    COrg,
    active_contract,
    create_contract,
    err,
    ok,
    upload_signed,
)

# ---------------------------------------------------------------- transition matrix (pure)
STATUSES = ("draft", "pending_approval", "active", "expired", "terminated")
KINDS = ("user", "system", "ai", "workflow", "api_key")


@pytest.mark.parametrize("src", STATUSES)
@pytest.mark.parametrize("dst", STATUSES)
@pytest.mark.parametrize("kind", KINDS)
def test_transition_matrix(src: str, dst: str, kind: str) -> None:
    allowed = contracts.TRANSITIONS.get((src, dst))
    actor = ActorRef(kind, uuid.uuid4() if kind != "system" else None)  # type: ignore[arg-type]
    if allowed is not None and kind in allowed:
        contracts.check_transition(src, dst, actor)
        return
    with pytest.raises((ConflictError, Forbidden)) as exc:
        contracts.check_transition(src, dst, actor)
    if kind in ("ai", "workflow", "api_key") and dst in ("active", "terminated"):
        assert exc.value.code == "human_only_action"


def test_table_is_prd_5_3() -> None:
    assert set(contracts.TRANSITIONS) == {
        (None, "draft"),
        ("draft", "pending_approval"),
        ("pending_approval", "draft"),
        ("draft", "active"),
        ("pending_approval", "active"),
        ("active", "expired"),
        ("active", "terminated"),
        ("expired", "active"),
    }


# ---------------------------------------------------------------- API flows
async def test_msa_activate_requires_signed_document(corg: COrg) -> None:
    events: list[ContractStatusChanged] = []
    subscribe(ContractStatusChanged, events.append)
    try:
        c = await create_contract(corg)
        assert c["status"] == "draft" and c["human_id"].startswith("CON-")
        err(
            await corg.c.post(f"{API}/contracts/{c['id']}/activate", headers=corg.h()),
            409,
            "signed_document_required",
        )
        doc = await upload_signed(corg, c["id"])
        assert doc["kind"] == "signed"
        active = await ok(await corg.c.post(f"{API}/contracts/{c['id']}/activate", headers=corg.h()))
        assert active["status"] == "active" and active["signed_file_id"] == doc["file_id"]
        assert [(e.from_status, e.to_status) for e in events if e.contract_id == uuid.UUID(c["id"])] == [
            ("draft", "active")
        ]
        # Active: only owner / renewal lead / notice period are editable.
        err(
            await corg.c.patch(f"{API}/contracts/{c['id']}", json={"title": "X"}, headers=corg.h()),
            409,
            "contract_not_editable",
        )
        ok_patch = await ok(
            await corg.c.patch(
                f"{API}/contracts/{c['id']}", json={"notice_period_days": 45}, headers=corg.h()
            )
        )
        assert ok_patch["notice_period_days"] == 45
        # The signed document of an active contract stays.
        err(
            await corg.c.delete(f"{API}/contract-documents/{doc['id']}", headers=corg.h()),
            409,
            "signed_document_locked",
        )
        listed = await ok(await corg.c.get(f"{API}/clients/{corg.client_id}/contracts"))
        assert [x["id"] for x in listed] == [c["id"]]
        term = await ok(
            await corg.c.post(
                f"{API}/contracts/{c['id']}/terminate",
                json={"reason": "Client exit", "termination_date": "2026-11-30"},
                headers=corg.h(),
            )
        )
        assert term["status"] == "terminated" and term["termination_reason"] == "Client exit"
        err(
            await corg.c.post(f"{API}/contracts/{c['id']}/activate", headers=corg.h()),
            409,
            "transition_not_allowed",
        )
        history = await ok(await corg.c.get(f"{API}/contracts/{c['id']}/history"))
        assert any("active -> terminated" in h["body"] for h in history)
    finally:
        unsubscribe(ContractStatusChanged, events.append)


async def test_sow_parent_rules(corg: COrg, other_corg: COrg) -> None:
    msa = await active_contract(corg)
    # SOW without parent is accepted as a draft but refused at activation.
    sow = await create_contract(corg, contract_type="sow", title="SOW 1")
    await upload_signed(corg, sow["id"])
    err(
        await corg.c.post(f"{API}/contracts/{sow['id']}/activate", headers=corg.h()),
        422,
        "parent_contract_invalid",
    )
    # Parent of another client.
    from tests.modules.demands.helpers import create_client

    other_client = await create_client(corg.admin)
    bad = await corg.c.post(
        f"{API}/contracts",
        json={
            "contract_type": "sow",
            "client_id": str(other_client),
            "legal_entity_id": str(corg.entity_id),
            "title": "x",
            "start_date": "2026-01-01",
            "parent_contract_id": msa["id"],
        },
        headers=corg.h(),
    )
    err(bad, 422, "parent_contract_invalid")
    ok_sow = await create_contract(
        corg,
        contract_type="sow",
        title="SOW 2",
        parent_contract_id=msa["id"],
        pricing_model="fixed_price",  # Epic 45: required to activate a SOW
        value_cap="900000",
    )
    await upload_signed(corg, ok_sow["id"])
    active = await ok(await corg.c.post(f"{API}/contracts/{ok_sow['id']}/activate", headers=corg.h()))
    assert active["status"] == "active"
    detail = await ok(await corg.c.get(f"{API}/contracts/{msa['id']}"))
    assert [ch["id"] for ch in detail["children"]] == [ok_sow["id"]]


async def _chain(org: COrg) -> None:
    async with tenant_session(org.tenant_id) as db:
        chain = ApprovalChain(
            id=uuid.uuid4(),
            tenant_id=org.tenant_id,
            subject_type="contract",
            name="Contracts",
            is_active=True,
        )
        db.add(chain)
        await db.flush()
        db.add(
            ApprovalChainStep(
                id=uuid.uuid4(),
                tenant_id=org.tenant_id,
                chain_id=chain.id,
                position=1,
                name="Legal",
                approver_user_id=org.admin.user_id,
                approver_kind="user",
            )
        )


async def test_approval_chain_flow(corg: COrg) -> None:
    await _chain(corg)
    c = await create_contract(corg)
    await upload_signed(corg, c["id"])
    err(await corg.c.post(f"{API}/contracts/{c['id']}/activate", headers=corg.h()), 409, "approval_pending")
    pending = await ok(await corg.c.post(f"{API}/contracts/{c['id']}/submit-for-approval", headers=corg.h()))
    assert pending["status"] == "pending_approval" and pending["approval_request_id"]
    decided = await ok(
        await corg.c.post(
            f"{API}/approvals/{pending['approval_request_id']}/decide",
            json={"action": "approve"},
            headers=corg.h(),
        )
    )
    assert decided["status"] == "approved"
    # Final approval with a signed document activates in the decision transaction.
    final = await ok(await corg.c.get(f"{API}/contracts/{c['id']}"))
    assert final["status"] == "active" and final["approved_at"]


async def test_approval_rejection_returns_to_draft(corg: COrg) -> None:
    await _chain(corg)
    c = await create_contract(corg)
    pending = await ok(await corg.c.post(f"{API}/contracts/{c['id']}/submit-for-approval", headers=corg.h()))
    await ok(
        await corg.c.post(
            f"{API}/approvals/{pending['approval_request_id']}/decide",
            json={"action": "reject", "comment": "Fix terms"},
            headers=corg.h(),
        )
    )
    assert (await ok(await corg.c.get(f"{API}/contracts/{c['id']}")))["status"] == "draft"


async def test_ai_actor_cannot_activate(corg: COrg) -> None:
    c = await create_contract(corg)
    await upload_signed(corg, c["id"])
    from app.core.context import RequestContext

    ai_ctx = RequestContext(tenant_id=corg.tenant_id, principal=corg.ctx().principal, actor_kind="ai")
    async with tenant_session(corg.tenant_id) as db:
        with pytest.raises(Forbidden) as exc:
            await contracts.activate(ai_ctx, db, uuid.UUID(c["id"]))
    assert exc.value.code == "human_only_action"


async def test_amendment_reactivates_expired_parent(corg: COrg) -> None:
    msa = await active_contract(corg, start_date="2025-01-01", end_date="2025-12-31")
    ctx = corg.ctx()
    async with tenant_session(corg.tenant_id) as db:
        assert await contracts.expire_due(ctx, db, datetime(2026, 1, 2, tzinfo=UTC)) == 1
    assert (await ok(await corg.c.get(f"{API}/contracts/{msa['id']}")))["status"] == "expired"
    amd = await create_contract(
        corg,
        contract_type="amendment",
        title="Extension",
        parent_contract_id=msa["id"],
        start_date="2026-01-01",
        end_date=None,
        amends_end_date="2030-12-31",
    )
    await upload_signed(corg, amd["id"])
    await ok(await corg.c.post(f"{API}/contracts/{amd['id']}/activate", headers=corg.h()))
    parent = await ok(await corg.c.get(f"{API}/contracts/{msa['id']}"))
    assert parent["status"] == "active" and parent["end_date"] == "2030-12-31"


async def test_expiry_uses_le_timezone(corg: COrg) -> None:
    msa = await active_contract(corg, start_date="2025-01-01", end_date="2026-10-04")
    ctx = corg.ctx()
    async with tenant_session(corg.tenant_id) as db:
        # 2026-10-04 18:00 UTC = 23:30 IST on the 4th: still the end date in Kolkata.
        assert await contracts.expire_due(ctx, db, datetime(2026, 10, 4, 18, 0, tzinfo=UTC)) == 0
        # 18:31 UTC = 00:01 IST on the 5th: expired.
        assert await contracts.expire_due(ctx, db, datetime(2026, 10, 4, 18, 31, tzinfo=UTC)) == 1
    assert (await ok(await corg.c.get(f"{API}/contracts/{msa['id']}")))["status"] == "expired"


async def test_renewal_reminders_deduped(corg: COrg) -> None:
    c = await active_contract(corg, start_date="2026-01-01", end_date="2026-12-31")
    seen: list[ContractRenewalDue] = []
    subscribe(ContractRenewalDue, seen.append)
    try:
        ctx = corg.ctx()
        at = datetime(2026, 11, 15, 6, tzinfo=UTC)  # 46 days before the end: inside the 60-day lead
        async with tenant_session(corg.tenant_id) as db:
            assert await contracts.renewal_reminders(ctx, db, at) == 1
        async with tenant_session(corg.tenant_id) as db:
            assert await contracts.renewal_reminders(ctx, db, at) == 0
        async with tenant_session(corg.tenant_id) as db:
            assert (
                await contracts.renewal_reminders(ctx, db, datetime(2026, 12, 5, 6, tzinfo=UTC)) == 1
            )  # 30-day lead
    finally:
        unsubscribe(ContractRenewalDue, seen.append)
    assert [e.lead_days for e in seen if e.contract_id == uuid.UUID(c["id"])] == [60, 30]


async def test_signature_completion_activates_draft(corg: COrg) -> None:
    from app.services import files_service

    c = await create_contract(corg)
    ctx = corg.ctx()
    async with tenant_session(corg.tenant_id) as db:
        stored = await files_service.store_upload(
            ctx, db, PDF, "final.pdf", {"pdf"}, "contract", uuid.UUID(c["id"])
        )
        file_id = stored.id
    async with tenant_session(corg.tenant_id) as db:
        assert (
            await contracts.on_signature_completed(ctx, db, uuid.UUID(c["id"]), uuid.uuid4(), file_id) is True
        )
    out = await ok(await corg.c.get(f"{API}/contracts/{c['id']}"))
    assert out["status"] == "active" and out["signed_file_id"] == str(file_id)


async def test_signature_request_unavailable_without_document(corg: COrg) -> None:
    c = await create_contract(corg)
    resp = await corg.c.post(
        f"{API}/contracts/{c['id']}/signature-request",
        json={"document_file_id": str(uuid.uuid4()), "signers": [{"email": "s@client.example.com"}]},
        headers=corg.h(),
    )
    err(resp, 409, "esign_unavailable")


async def test_signature_request_links_uploaded_contract_document(corg: COrg) -> None:
    """P2 fix: a draft uploaded to the contract becomes a documents row on first send, so eSign works."""
    c = await create_contract(corg)
    up = await ok(
        await corg.c.post(
            f"{API}/contracts/{c['id']}/documents",
            files={"file": ("draft.pdf", PDF, "application/pdf")},
            data={"kind": "draft"},
            headers=corg.h(),
        ),
        201,
    )
    signers = [{"email": "s@client.example.com", "name": "Signer"}]
    body = {"document_file_id": up["file_id"], "signers": signers}
    url = f"{API}/contracts/{c['id']}/signature-request"
    out = await ok(await corg.c.post(url, json=body, headers=corg.h()))
    assert out["signature_request_id"]
    # A file that is not this contract's still answers esign_unavailable.
    other = await create_contract(corg)
    resp = await corg.c.post(f"{API}/contracts/{other['id']}/signature-request", json=body, headers=corg.h())
    err(resp, 409, "esign_unavailable")


async def test_public_helpers_and_crm_draft(corg: COrg) -> None:
    msa = await active_contract(corg, placement_fee_pct="8.33", reverse_charge=True, share_bgv_outcome=True)
    ctx = corg.ctx()
    async with tenant_session(corg.tenant_id) as db:
        summary = await commercials_service.active_contract_for(
            ctx, db, corg.client_id, corg.entity_id, date(2026, 6, 1)
        )
        assert summary is not None and str(summary.id) == msa["id"]
        assert (
            await commercials_service.active_contract_for(
                ctx, db, corg.client_id, corg.entity_id, date(2030, 6, 1)
            )
            is None
        )
        facts = await commercials_service.get_contract(ctx, db, uuid.UUID(msa["id"]))
        assert facts.placement_fee_pct == Decimal("8.3300") and facts.reverse_charge is True
        assert await commercials_service.shares_bgv_outcome(ctx, db, uuid.UUID(msa["id"])) is True
        with pytest.raises(Exception) as exc:
            await commercials_service.assert_contract_active(
                ctx, db, uuid.UUID(msa["id"]), uuid.uuid4(), corg.entity_id, date(2026, 6, 1)
            )
        assert getattr(exc.value, "code", None) == "contract_not_active"
        draft = await commercials_service.create_contract_draft(
            ctx,
            db,
            ContractDraftIn(
                client_id=corg.client_id,
                legal_entity_id=corg.entity_id,
                contract_type="sow",
                start_date=date(2026, 11, 1),
                source_opportunity_id=uuid.uuid4(),
            ),
        )
        assert draft.status == "draft" and draft.contract_type == "sow"
        row = (await db.execute(select(Contract).where(Contract.id == draft.id))).scalar_one()
        assert row.opportunity_id is not None


async def test_milestones_and_posting_loader(corg: COrg) -> None:
    from app.services.postings import SourceQuery, posting_sources

    msa = await active_contract(corg)
    sow = await create_contract(
        corg,
        contract_type="sow",
        title="SOW",
        parent_contract_id=msa["id"],
        pricing_model="milestone",  # Epic 45: required to activate a SOW
        value_cap="750000",
    )
    err(
        await corg.c.post(
            f"{API}/contracts/{msa['id']}/milestones", json={"name": "x", "amount": "10"}, headers=corg.h()
        ),
        409,
        "milestones_sow_only",
    )
    m1 = await ok(
        await corg.c.post(
            f"{API}/contracts/{sow['id']}/milestones",
            json={"name": "Design", "amount": "250000"},
            headers=corg.h(),
        ),
        201,
    )
    m2 = await ok(
        await corg.c.post(
            f"{API}/contracts/{sow['id']}/milestones",
            json={"name": "Build", "amount": "500000"},
            headers=corg.h(),
        ),
        201,
    )
    err(
        await corg.c.post(f"{API}/contract-milestones/{m1['id']}/accept", headers=corg.h()),
        409,
        "contract_not_active",
    )
    await upload_signed(corg, sow["id"])
    await ok(await corg.c.post(f"{API}/contracts/{sow['id']}/activate", headers=corg.h()))
    accepted = await ok(await corg.c.post(f"{API}/contract-milestones/{m1['id']}/accept", headers=corg.h()))
    assert accepted["status"] == "accepted" and accepted["amount"] == {
        "amount": "250000.00",
        "currency": "INR",
    }
    loader = posting_sources()["milestone"].loader
    async with tenant_session(corg.tenant_id) as db:
        items = await loader(corg.ctx(), db, SourceQuery(ledger="billing", period_end=date(2099, 1, 1)))
    ids = {i.source_id for i in items}
    assert uuid.UUID(m1["id"]) in ids and uuid.UUID(m2["id"]) not in ids
    item = next(i for i in items if i.source_id == uuid.UUID(m1["id"]))
    assert item.quantity == Decimal("1") and item.amount == Decimal("250000.00")
    cancelled = await ok(await corg.c.post(f"{API}/contract-milestones/{m2['id']}/cancel", headers=corg.h()))
    assert cancelled["status"] == "cancelled"
    listed = await ok(await corg.c.get(f"{API}/contracts/{sow['id']}/milestones"))
    assert {m["name"]: m["billed"] for m in listed} == {"Design": False, "Build": False}
