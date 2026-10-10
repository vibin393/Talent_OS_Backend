"""Rate checks + the rate-exception gate (FR-91) and its P1 wiring: Demand submit, Submission create, Offer
release each blocked, approved by Finance, then pass (Epic 16 A6)."""

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.core.database import tenant_session
from app.core.errors import ConflictError
from app.schemas.commercials import RateCheckRequest
from app.services import commercials_service
from tests.modules.commercials.helpers import (
    API,
    COrg,
    admin_approves_exceptions,
    err,
    line,
    ok,
    published_card,
)
from tests.modules.demands.helpers import create_demand
from tests.modules.offers.helpers import create_offer, setup_selected, submit
from tests.modules.pipeline.helpers import (
    add_one,
    create_candidate,
    create_contact,
    per_app,
    shortlist,
    upload_parsed_resume,
)


def req(org: COrg, subject_id: uuid.UUID, bill: str = "1950", **kw: object) -> RateCheckRequest:
    body: dict[str, object] = {
        "subject_kind": "placement",
        "subject_id": subject_id,
        "client_id": org.client_id,
        "on_date": date(2026, 6, 1),
        "role": "Java Developer",
        "location": "Bengaluru",
        "experience_years": Decimal("4"),
        "bill_rate": Decimal(bill),
        "currency": "INR",
        "rate_unit": "hour",
    }
    body.update(kw)
    return RateCheckRequest(**body)  # type: ignore[arg-type]


async def approve(org: COrg, request_id: str, action: str = "approve") -> None:
    body = {"action": action, **({"comment": "Rate too high"} if action != "approve" else {})}
    await ok(await org.c.post(f"{API}/approvals/{request_id}/decide", json=body, headers=org.h()))


async def test_validate_rate_and_preview(corg: COrg) -> None:
    ctx = corg.ctx()
    subject = uuid.uuid4()
    async with tenant_session(corg.tenant_id) as db:
        res = await commercials_service.validate_rate(ctx, db, req(corg, subject))
    assert (
        res.status == "not_applicable" and res.requires_exception is False and res.rate_check_id is not None
    )
    await published_card(corg)
    async with tenant_session(corg.tenant_id) as db:
        res = await commercials_service.validate_rate(ctx, db, req(corg, subject, bill="1500"))
        assert res.status == "within" and res.max_bill_rate == Decimal("1800.0000")
        res = await commercials_service.validate_rate(ctx, db, req(corg, None, bill="1950"))  # type: ignore[arg-type]
        assert res.status == "outside" and res.rate_check_id is None
    preview = await ok(
        await corg.c.post(
            f"{API}/rate-checks/preview",
            json={
                "subject_kind": "demand",
                "subject_id": None,
                "client_id": str(corg.client_id),
                "on_date": "2026-06-01",
                "role": "Python Dev",
                "bill_rate": "100",
                "currency": "INR",
                "rate_unit": "hour",
            },
            headers=corg.h(),
        )
    )
    assert preview["result"] == "no_matching_line" and preview["requires_exception"] is True
    history = await ok(
        await corg.c.get(
            f"{API}/rate-checks", params={"subject_kind": "placement", "subject_id": str(subject)}
        )
    )
    assert [h["result"] for h in history] == ["ok", "not_validated"]


async def test_gate_starts_once_then_pending_approved_and_regates(corg: COrg) -> None:
    await admin_approves_exceptions(corg)
    await published_card(corg)
    ctx = corg.ctx()
    subject = uuid.uuid4()
    async with tenant_session(corg.tenant_id) as db:
        with pytest.raises(ConflictError) as exc:
            await commercials_service.require_rate_clearance(ctx, db, req(corg, subject))
    assert exc.value.code == "rate_exception_required"
    details = exc.value.details
    assert details["result"] == "out_of_range" and details["min"] == "1200" and details["max"] == "1800"
    request_id = details["approval_request_id"]
    async with tenant_session(corg.tenant_id) as db:
        with pytest.raises(ConflictError) as exc:
            await commercials_service.require_rate_clearance(ctx, db, req(corg, subject))
    assert (
        exc.value.code == "rate_exception_pending" and exc.value.details["approval_request_id"] == request_id
    )
    await approve(corg, request_id)
    async with tenant_session(corg.tenant_id) as db:
        res = await commercials_service.require_rate_clearance(ctx, db, req(corg, subject))
    assert res.status == "outside" and res.requires_exception is True
    # A changed rate is a new check -> a new exception.
    async with tenant_session(corg.tenant_id) as db:
        with pytest.raises(ConflictError) as exc:
            await commercials_service.require_rate_clearance(ctx, db, req(corg, subject, bill="2100"))
    assert (
        exc.value.code == "rate_exception_required" and exc.value.details["approval_request_id"] != request_id
    )
    await approve(corg, exc.value.details["approval_request_id"], "reject")
    async with tenant_session(corg.tenant_id) as db:
        with pytest.raises(ConflictError) as exc:
            await commercials_service.require_rate_clearance(ctx, db, req(corg, subject, bill="2100"))
    assert exc.value.code == "rate_exception_rejected"


async def test_no_card_or_no_bill_rate_passes(corg: COrg) -> None:
    async with tenant_session(corg.tenant_id) as db:
        res = await commercials_service.require_rate_clearance(corg.ctx(), db, req(corg, uuid.uuid4()))
    assert res.status == "not_applicable"


# ---------------------------------------------------------------- P1 wiring
async def test_demand_submit_gate(corg: COrg) -> None:
    await admin_approves_exceptions(corg)
    await published_card(
        corg,
        lines=[
            line(
                role="Senior Data Engineer",
                location="Chennai",
                experience_min_years=None,
                experience_max_years=None,
                max_bill_rate="1400",
            )
        ],
    )
    demand = await create_demand(corg.admin, corg.client_id)  # bill rate 1500.00 / hour INR
    blocked = err(
        await corg.c.post(f"{API}/demands/{demand['id']}/submit", headers=corg.h()),
        409,
        "rate_exception_required",
    )
    await approve(corg, blocked["details"]["approval_request_id"])
    opened = await ok(await corg.c.post(f"{API}/demands/{demand['id']}/submit", headers=corg.h()))
    assert opened["status"] == "open"
    history = await ok(
        await corg.c.get(f"{API}/rate-checks", params={"subject_kind": "demand", "subject_id": demand["id"]})
    )
    assert history[0]["exception_status"] == "approved"


async def test_submission_gate(corg: COrg) -> None:
    await admin_approves_exceptions(corg)
    from tests.modules.demands.helpers import open_demand

    demand = await open_demand(corg.admin, corg.client_id)
    contact_id, _ = await create_contact(corg.admin, corg.client_id)
    cand = await create_candidate(corg.admin)
    await upload_parsed_resume(corg.admin, corg.tenant_id, cand["id"])
    app_id = await add_one(corg.admin, demand["id"], cand["id"])
    await shortlist(corg.admin, app_id)
    await published_card(
        corg,
        lines=[
            line(
                role="Senior Data Engineer",
                location="Chennai",
                experience_min_years=None,
                experience_max_years=None,
            )
        ],
    )
    payload = {
        "application_ids": [app_id],
        "per_application": {
            app_id: per_app(
                bill="1950.00",
                consent_attestation={"text": "Candidate agreed by phone on 1 Oct", "confirmed": True},
            )
        },
        "recipient_contact_ids": [str(contact_id)],
    }
    url = f"{API}/demands/{demand['id']}/submissions"
    blocked = err(await corg.c.post(url, json=payload, headers=corg.h()), 409, "rate_exception_required")
    assert blocked["details"]["subject_kind"] == "submission" and blocked["details"]["subject_id"] == app_id
    await approve(corg, blocked["details"]["approval_request_id"])
    sent = await ok(await corg.c.post(url, json=payload, headers=corg.h()))
    assert sent["items"][0]["status"] in ("sent", "pending_approval")


async def test_offer_release_gate(corg: COrg) -> None:
    await admin_approves_exceptions(corg)
    from app.services.pipeline import submissions as _  # noqa: F401 - ensure P1 modules are loaded

    s = await setup_selected(corg)  # type: ignore[arg-type]
    offer = await create_offer(
        corg.admin, s.application_id, bill_rate={"amount": "1950.00", "currency": "INR"}
    )
    approved = await submit(corg.admin, offer["id"])
    assert approved["status"] == "approved"
    await published_card(
        corg,
        lines=[
            line(
                role="Senior Data Engineer",
                location=None,
                experience_min_years=None,
                experience_max_years=None,
            )
        ],
    )
    blocked = err(
        await corg.c.post(f"{API}/offers/{offer['id']}/release", headers=corg.h()),
        409,
        "rate_exception_required",
    )
    await approve(corg, blocked["details"]["approval_request_id"])
    released = await ok(await corg.c.post(f"{API}/offers/{offer['id']}/release", headers=corg.h()))
    assert released["status"] == "released"
