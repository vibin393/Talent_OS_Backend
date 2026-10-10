"""AD-24 approval engine extensions (Epic 13, story 13.6 + R5 / R19): explicit steps, approver resolvers,
external / attributed actors, callbacks in the decision transaction, org grants."""

import dataclasses
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text

from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.database import OrgScope, tenant_session
from app.core.errors import DomainError, Forbidden, ValidationFailed
from app.core.pagination import PageParams
from app.core.permissions import CATALOG, Principal, Scope
from app.models.approvals import ApprovalDecision, ApprovalRequest
from app.models.files import StoredFile
from app.models.placements import Placement
from app.schemas.approvals import DecideIn
from app.services import approvals, directory_service
from app.services.approvals import EscalationSpec, FinalDecision, StepDecision, StepSpec
from tests.epic3 import create_candidate
from tests.modules.demands.helpers import DOrg, create_client
from tests.modules.legal_entities.conftest import provision
from tests.p2 import make_client_user

SUBJECT = "p13_test_subject"


class Calls:
    def __init__(self) -> None:
        self.steps: list[StepDecision] = []
        self.finals: list[FinalDecision] = []
        self.raise_on_final: Exception | None = None
        self.skip_next = False
        self.seen_in_tx: list[str] = []


@pytest.fixture
def calls() -> Iterator[Calls]:
    c = Calls()

    async def on_step(ctx: RequestContext, db: Any, d: StepDecision) -> None:
        c.steps.append(d)
        # the decision row is visible inside the same transaction
        found = (
            (
                await db.execute(
                    select(ApprovalDecision.actor_kind).where(ApprovalDecision.request_id == d.request_id)
                )
            )
            .scalars()
            .all()
        )
        c.seen_in_tx.extend(found)
        if c.skip_next and not d.is_final:
            c.skip_next = False
            await approvals.skip_current_step(ctx, db, d.request_id, reason="not needed")

    async def on_final(ctx: RequestContext, db: Any, d: FinalDecision) -> None:
        c.finals.append(d)
        if c.raise_on_final is not None:
            raise c.raise_on_final

    approvals.register_approval_subject(SUBJECT, on_final_decided=on_final, on_step_decided=on_step)
    yield c
    approvals.unregister_approval_subject(SUBJECT)


async def _admin_ctx(org: DOrg) -> RequestContext:
    async with tenant_session(org.tenant_id) as db:
        role = await directory_service.role_id_by_key(db, "tenant_admin")
    assert role is not None
    principal = Principal(
        type="user",
        id=org.admin.user_id,
        tenant_id=org.tenant_id,
        role_ids=frozenset({role}),
        permissions=dict.fromkeys(CATALOG, Scope.TENANT),
    )
    return RequestContext(tenant_id=org.tenant_id, principal=principal)


def _client_ctx(
    org: DOrg, client_user_id: uuid.UUID, client_id: uuid.UUID, perms: frozenset[str] = frozenset()
) -> RequestContext:
    return RequestContext(
        tenant_id=org.tenant_id,
        principal=Principal(
            type="client_user",
            id=client_user_id,
            tenant_id=org.tenant_id,
            org_id=client_id,
            portal_permissions=perms,
        ),
    )


def _approve(comment: str | None = None, payload: dict[str, Any] | None = None) -> DecideIn:
    return DecideIn(action="approve", comment=comment, subject_payload=payload)


async def _start(
    ctx: RequestContext, steps: list[StepSpec], facts: dict[str, Any] | None = None
) -> uuid.UUID:
    async with tenant_session(ctx.tenant_id) as db:  # type: ignore[arg-type]
        result = await approvals.start(ctx, db, SUBJECT, uuid.uuid4(), facts or {}, steps=steps)
    assert result.status == "pending" and result.request_id is not None
    return result.request_id


async def _request(tenant_id: uuid.UUID, request_id: uuid.UUID) -> ApprovalRequest:
    async with tenant_session(tenant_id) as db:
        row = await db.get(ApprovalRequest, request_id)
        assert row is not None
        db.expunge(row)
        return row


async def _evidence(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_session(tenant_id) as db:
        f = StoredFile(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            storage_key=f"t13/{uuid.uuid4().hex}",
            original_name="approval-email.pdf",
            content_type="application/pdf",
            size_bytes=10,
            sha256="0" * 64,
            scan_status="clean",
        )
        db.add(f)
        return f.id


@pytest.fixture
async def org(dorg: DOrg) -> AsyncIterator[DOrg]:
    yield dorg


# ---------------------------------------------------------------- resolvers
async def test_explicit_user_then_client_user_in_client_session(org: DOrg, calls: Calls) -> None:
    admin = await _admin_ctx(org)
    cu = await make_client_user(org.tenant_id, org.client_id)
    esc = EscalationSpec(
        after_business_hours=Decimal("8"), action="notify", target_kind="manager_of_assignee"
    )
    rid = await _start(
        admin,
        [
            StepSpec("Account manager", "user", {"user_id": str(org.admin.user_id)}, escalation=esc),
            StepSpec("Client", "client_user", {"client_user_id": str(cu)}),
        ],
    )
    req = await _request(org.tenant_id, rid)
    assert req.steps_source == "explicit" and req.current_approver_kind == "user"
    assert req.current_step_activated_at is not None and req.initiated_by_kind == "user"
    assert req.chain_snapshot[0]["escalation"]["after_business_hours"] == "8"
    assert req.chain_snapshot[1]["org"] == ["client", str(org.client_id)]

    async with tenant_session(org.tenant_id) as db:
        await approvals.decide(admin, db, rid, _approve())
    req = await _request(org.tenant_id, rid)
    assert req.current_approver_kind == "client_user" and req.current_approver_client_user_id == cu

    client_ctx = _client_ctx(org, cu, org.client_id)
    async with tenant_session(org.tenant_id, org=OrgScope.of("client", org.client_id)) as db:
        pending = await approvals.list_my_pending(client_ctx, db, PageParams())
        assert [p.id for p in pending.items] == [rid]
        out = await approvals.decide(client_ctx, db, rid, _approve(payload={"lines": [1]}))
        assert out.status == "approved"
    assert [s.actor.kind for s in calls.steps] == ["user", "client_user"]
    assert calls.finals[-1].actor == calls.steps[-1].actor == ActorRef("client_user", cu)
    assert calls.finals[-1].payload == {"lines": [1]} and calls.steps[-1].payload == {"lines": [1]}
    assert "client_user" in calls.seen_in_tx


async def test_client_sees_only_granted_requests(org: DOrg, calls: Calls) -> None:
    admin = await _admin_ctx(org)
    other_client = await create_client(org.admin)
    cu = await make_client_user(org.tenant_id, org.client_id)
    cu_other = await make_client_user(org.tenant_id, other_client)
    mine = await _start(admin, [StepSpec("Client", "client_user", {"client_user_id": str(cu)})])
    await _start(admin, [StepSpec("Client", "client_user", {"client_user_id": str(cu_other)})])
    await _start(admin, [StepSpec("Internal", "user", {"user_id": str(org.admin.user_id)})])
    async with tenant_session(org.tenant_id, org=OrgScope.of("client", org.client_id)) as db:
        visible = (await db.execute(select(ApprovalRequest.id))).scalars().all()
        assert visible == [mine]
        grants = (await db.execute(text("SELECT subject_id FROM org_grants"))).scalars().all()
        assert grants == [mine]
        # another client's user cannot decide here
        with pytest.raises(Forbidden):
            await approvals.decide(_client_ctx(org, cu_other, other_client), db, mine, _approve())
    async with tenant_session(org.tenant_id) as db:
        assert (
            len(
                (
                    await db.execute(
                        select(ApprovalRequest.id).where(ApprovalRequest.subject_type == SUBJECT)
                    )
                ).all()
            )
            == 3
        )


async def test_role_resolvers_by_id_key_and_finance(org: DOrg, calls: Calls) -> None:
    admin = await _admin_ctx(org)
    async with tenant_session(org.tenant_id) as db:
        role_id = await directory_service.role_id_by_key(db, "tenant_admin")
    rid = await _start(
        admin,
        [
            StepSpec("By id", "role", {"role_id": str(role_id)}),
            StepSpec("By key", "role", {"role_key": "tenant_admin"}),
            StepSpec("Finance", "finance_role", {}),
        ],
    )
    req = await _request(org.tenant_id, rid)
    assert [s["resolved_kind"] for s in req.chain_snapshot] == ["role", "role", "role"]
    assert req.chain_snapshot[1]["approver_role_id"] == str(role_id)
    # The finance step resolves to the seeded ``finance_admin`` role (tenant_admin only when it is missing).
    async with tenant_session(org.tenant_id) as db:
        finance_id = await directory_service.role_id_by_key(db, "finance_admin")
    assert finance_id is not None
    assert req.chain_snapshot[2]["approver_role_id"] == str(finance_id)
    for _ in range(2):
        async with tenant_session(org.tenant_id) as db:
            out = await approvals.decide(admin, db, rid, _approve())
    async with tenant_session(org.tenant_id) as db:
        with pytest.raises(Forbidden):  # a tenant_admin is not a finance approver
            await approvals.decide(admin, db, rid, _approve())
    assert isinstance(admin.principal, Principal)
    finance = RequestContext(
        tenant_id=org.tenant_id,
        principal=dataclasses.replace(admin.principal, role_ids=frozenset({finance_id})),
    )
    async with tenant_session(org.tenant_id) as db:
        out = await approvals.decide(finance, db, rid, _approve())
    assert out.status == "approved"


async def test_unresolvable_steps(org: DOrg, calls: Calls) -> None:
    admin = await _admin_ctx(org)
    async with tenant_session(org.tenant_id) as db:
        for spec, facts in (
            (StepSpec("AM", "placement_account_manager", {}), {"placement_id": str(uuid.uuid4())}),
            (StepSpec("Who", "no_such_kind", {}), {}),
            (StepSpec("Role", "role", {"role_key": "no_such_role"}), {}),
            (StepSpec("Client", "client_user", {"client_user_id": str(uuid.uuid4())}), {}),
        ):
            with pytest.raises(ValidationFailed) as exc:
                await approvals.start(admin, db, SUBJECT, uuid.uuid4(), facts, steps=[spec])
            assert exc.value.code == "approver_unresolved" and exc.value.details == {"step": spec.name}


async def test_placement_account_manager_resolver(org: DOrg, calls: Calls) -> None:
    admin = await _admin_ctx(org)
    le = await provision(org.tenant)
    cand = await create_candidate(org.admin, full_name="Pia Placed")
    placement_id = uuid.uuid4()
    async with tenant_session(org.tenant_id) as db:
        db.add(
            Placement(
                id=placement_id,
                tenant_id=org.tenant_id,
                human_id=f"PLC-{uuid.uuid4().hex[:6]}",
                candidate_id=uuid.UUID(cand["id"]),
                client_id=org.client_id,
                legal_entity_id=le,
                worker_type="employee",
                payee_kind="worker",
                planned_start_date=date(2026, 10, 1),
                currency="INR",
                account_manager_user_id=org.admin.user_id,
            )
        )
    rid = await _start(
        admin, [StepSpec("AM", "placement_account_manager", {})], {"placement_id": str(placement_id)}
    )
    req = await _request(org.tenant_id, rid)
    assert req.current_approver_user_id == org.admin.user_id


async def test_client_any_with_portal_permission(org: DOrg, calls: Calls) -> None:
    admin = await _admin_ctx(org)
    holder = await make_client_user(org.tenant_id, org.client_id, permissions=("time:approve",))
    lacking = await make_client_user(org.tenant_id, org.client_id, permissions=("invoices:view",))
    rid = await _start(
        admin,
        [
            StepSpec(
                "Any client",
                "client_any",
                {"client_id": str(org.client_id), "portal_permission": "time:approve"},
            )
        ],
    )
    async with tenant_session(org.tenant_id, org=OrgScope.of("client", org.client_id)) as db:
        with pytest.raises(Forbidden):
            await approvals.decide(
                _client_ctx(org, lacking, org.client_id, frozenset({"invoices:view"})), db, rid, _approve()
            )
    async with tenant_session(org.tenant_id, org=OrgScope.of("client", org.client_id)) as db:
        ctx = _client_ctx(org, holder, org.client_id, frozenset({"time:approve"}))
        pending = await approvals.list_my_pending(ctx, db, PageParams())
        assert [p.id for p in pending.items] == [rid]
        assert (await approvals.decide(ctx, db, rid, _approve())).status == "approved"


# ---------------------------------------------------------------- attributed actors
async def test_secure_link_attribution(org: DOrg, calls: Calls) -> None:
    admin = await _admin_ctx(org)
    cu = await make_client_user(org.tenant_id, org.client_id)
    rid = await _start(admin, [StepSpec("Client", "client_user", {"client_user_id": str(cu)})])
    link = uuid.uuid4()
    system_ctx = RequestContext(tenant_id=org.tenant_id)
    async with tenant_session(org.tenant_id) as db:
        with pytest.raises(Forbidden):
            await approvals.decide(
                system_ctx,
                db,
                rid,
                _approve(),
                actor=ActorRef("secure_link", link, attributed_to=("client_user", uuid.uuid4())),
            )
    async with tenant_session(org.tenant_id) as db:
        out = await approvals.decide(
            system_ctx,
            db,
            rid,
            _approve(),
            actor=ActorRef("secure_link", link, attributed_to=("client_user", cu)),
        )
        assert out.status == "approved"
        row = (
            await db.execute(select(ApprovalDecision).where(ApprovalDecision.request_id == rid))
        ).scalar_one()
        assert row.actor_kind == "secure_link" and row.secure_link_id == link
        assert (row.attributed_kind, row.attributed_id) == ("client_user", cu)
    assert calls.finals[-1].actor is not None and calls.finals[-1].actor.kind == "secure_link"


async def test_recorded_by_user_requires_evidence_and_permission(org: DOrg, calls: Calls) -> None:
    admin = await _admin_ctx(org)
    cu = await make_client_user(org.tenant_id, org.client_id)
    rid = await _start(admin, [StepSpec("Client", "client_user", {"client_user_id": str(cu)})])
    me = org.admin.user_id
    async with tenant_session(org.tenant_id) as db:
        with pytest.raises(ValidationFailed) as exc:
            await approvals.decide(
                admin,
                db,
                rid,
                _approve(),
                actor=ActorRef("recorded_by_user", me, attributed_to=("client_user", cu)),
            )
        assert exc.value.code == "evidence_required"
    evidence = await _evidence(org.tenant_id)
    no_perm = RequestContext(
        tenant_id=org.tenant_id,
        principal=Principal(type="user", id=me, tenant_id=org.tenant_id, permissions={}),
    )
    async with tenant_session(org.tenant_id) as db:
        with pytest.raises(Forbidden):
            await approvals.decide(
                no_perm, db, rid, _approve(),
                actor=ActorRef("recorded_by_user", me, attributed_to=("client_user", cu), evidence_file_id=evidence),
            )  # fmt: skip
    async with tenant_session(org.tenant_id) as db:
        out = await approvals.decide(
            admin, db, rid, _approve("approved by phone"),
            actor=ActorRef("recorded_by_user", me, attributed_to=("client_user", cu), evidence_file_id=evidence),
        )  # fmt: skip
        assert out.status == "approved"
        row = (
            await db.execute(select(ApprovalDecision).where(ApprovalDecision.request_id == rid))
        ).scalar_one()
        assert (
            row.actor_kind == "recorded_by_user" and row.actor_id == me and row.evidence_file_id == evidence
        )


@pytest.mark.parametrize("kind", ["ai", "workflow", "system", "api_key", "candidate", "vendor_user"])
async def test_non_approver_kinds_refused(org: DOrg, calls: Calls, kind: str) -> None:
    admin = await _admin_ctx(org)
    rid = await _start(admin, [StepSpec("AM", "user", {"user_id": str(org.admin.user_id)})])
    async with tenant_session(org.tenant_id) as db:
        with pytest.raises(Forbidden) as exc:
            await approvals.decide(admin, db, rid, _approve(), actor=ActorRef(kind, uuid.uuid4()))  # type: ignore[arg-type]
        assert exc.value.code == "human_only_action"
        with pytest.raises(Forbidden):
            await approvals.decide(admin, db, rid, _approve(), actor_kind="workflow")


async def test_callback_exception_rolls_back_decision(org: DOrg, calls: Calls) -> None:
    admin = await _admin_ctx(org)
    rid = await _start(admin, [StepSpec("AM", "user", {"user_id": str(org.admin.user_id)})])
    calls.raise_on_final = ValidationFailed("subject refuses", code="subject_refuses")
    with pytest.raises(DomainError):
        async with tenant_session(org.tenant_id) as db:
            await approvals.decide(admin, db, rid, _approve())
    req = await _request(org.tenant_id, rid)
    assert req.status == "pending" and req.current_position == 1
    async with tenant_session(org.tenant_id) as db:
        assert (
            await db.execute(select(ApprovalDecision).where(ApprovalDecision.request_id == rid))
        ).first() is None
    calls.raise_on_final = None
    async with tenant_session(org.tenant_id) as db:
        assert (await approvals.decide(admin, db, rid, _approve())).status == "approved"


async def test_skip_current_step_from_step_callback(org: DOrg, calls: Calls) -> None:
    admin = await _admin_ctx(org)
    rid = await _start(
        admin,
        [
            StepSpec("One", "user", {"user_id": str(org.admin.user_id)}),
            StepSpec("Two", "user", {"user_id": str(org.admin.user_id)}),
            StepSpec("Three", "user", {"user_id": str(org.admin.user_id)}),
        ],
    )
    calls.skip_next = True
    async with tenant_session(org.tenant_id) as db:
        await approvals.decide(admin, db, rid, _approve())
    req = await _request(org.tenant_id, rid)
    assert req.current_position == 3
    async with tenant_session(org.tenant_id) as db:
        rows = (
            await db.execute(
                select(ApprovalDecision.outcome, ApprovalDecision.actor_kind)
                .where(ApprovalDecision.request_id == rid)
                .order_by(ApprovalDecision.position)
            )
        ).all()
        assert [tuple(r) for r in rows] == [("approved", "user"), ("skipped", "system")]
        with pytest.raises(RuntimeError):
            await approvals.skip_current_step(admin, db, rid, reason="outside a callback")


async def test_delegation_refused_without_resolver(org: DOrg, calls: Calls) -> None:
    admin = await _admin_ctx(org)
    cu = await make_client_user(org.tenant_id, org.client_id)
    rid = await _start(admin, [StepSpec("Client", "client_user", {"client_user_id": str(cu)})])
    other = await make_client_user(org.tenant_id, org.client_id)
    async with tenant_session(org.tenant_id, org=OrgScope.of("client", org.client_id)) as db:
        with pytest.raises(Forbidden) as exc:
            await approvals.decide(
                _client_ctx(org, other, org.client_id),
                db,
                rid,
                _approve(),
                actor=ActorRef("client_user", other, on_behalf_of=cu),
            )
        assert exc.value.code == "not_current_approver"


async def test_named_routes(org: DOrg, calls: Calls) -> None:
    admin = await _admin_ctx(org)

    async def route(ctx: RequestContext, db: Any, *, user_id: uuid.UUID) -> list[StepSpec]:
        return [StepSpec("Routed", "user", {"user_id": str(user_id)})]

    from app.services.approvals.routes import _routes

    with _routes.override("p13_route", route):
        async with tenant_session(org.tenant_id) as db:
            steps = await approvals.resolve_route(admin, db, "p13_route", user_id=org.admin.user_id)
            assert [s.name for s in steps] == ["Routed"]
            with pytest.raises(ValidationFailed) as exc:
                await approvals.resolve_route(admin, db, "nope")
            assert exc.value.code == "unknown_route"
