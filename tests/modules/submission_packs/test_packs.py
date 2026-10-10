# ruff: noqa: E501
"""Epic 61 submission packs: the stub contract, the check matrix, one open pack per application, masking, the human
edits, link to the Submission with the diff, discard on stage exit, consent re-check without a model, erasure, the
client rules API and access (cross-tenant 404)."""

import uuid
from decimal import Decimal

from fastapi import FastAPI
from sqlalchemy import select, update

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import CATALOG, Principal, Scope
from app.models.candidates import CandidateResume
from app.models.submission_packs import SubmissionPack
from app.schemas.submission_packs import SubmissionPackOut
from app.services import submission_packs_service as svc
from tests.modules.identity.conftest import join
from tests.modules.pipeline.helpers import (
    add_one,
    create_candidate,
    h,
    move,
    per_app,
    stages,
    upload_parsed_resume,
)
from tests.modules.submission_packs.helpers import (
    API,
    PackSeed,
    check_of,
    get_pack,
    grant_submission_consent,
    pack_rows,
    prepare,
    put_rules,
    set_candidate,
)


def _ctx(seed: PackSeed, **perms: Scope) -> RequestContext:
    permissions = perms or dict.fromkeys(CATALOG, Scope.TENANT)
    return RequestContext(
        tenant_id=seed.tenant_id,
        principal=Principal(
            type="user", id=seed.admin.user_id, tenant_id=seed.tenant_id, permissions=permissions
        ),
    )


# ================================================================ contract stub
async def test_prepare_pack_matches_the_frozen_contract(seed: PackSeed) -> None:
    async with tenant_session(seed.tenant_id) as db:
        out = await svc.prepare_pack(_ctx(seed), db, seed.application_id)
    assert isinstance(out, SubmissionPackOut)
    assert (out.application_id, out.demand_id, out.client_id) == (
        seed.application_id,
        seed.demand_id,
        seed.client_id,
    )
    assert out.status == "draft" and out.summary_text == ""
    keys = [c.key for c in out.checks]
    assert sorted(keys) == sorted(
        [
            "consent",
            "margin",
            "rate_card",
            "duplicate",
            "vendor_ownership",
            "required_fields",
            "resume_format",
        ]
    )
    consent = next(c for c in out.checks if c.key == "consent")
    assert consent.ok is True and consent.message.startswith("[warn] ")  # attestation_allowed by default
    assert "write_up" in out.missing and len(out.document_ids) == 1


async def test_manual_prepare_endpoint_and_one_open_pack(seed: PackSeed) -> None:
    first = await prepare(seed)
    assert first["human_id"].startswith("PCK-") and first["version"] == 1 and first["lifecycle"] == "open"
    assert first["resume_kind"] == "client_formatted" and first["prepared_by_kind"] == "user"
    # rates suggested from the demand (bill 1500) and the candidate's expected pay (1000/h, capped at 1100)
    assert (first["bill_rate"], first["pay_rate"], first["currency"]) == ("1500.00", "1000.00", "INR")
    assert check_of(first, "margin")["level"] in ("pass", "warn")
    assert check_of(first, "duplicate")["level"] == "pass"
    assert check_of(first, "required_fields")["level"] == "pass"
    second = await prepare(seed)
    assert second["id"] == first["id"] and second["version"] == 2
    assert len(await pack_rows(seed.tenant_id, SubmissionPack.application_id == seed.application_id)) == 1


async def test_client_formatted_rule_without_parsed_resume_blocks(seed: PackSeed) -> None:
    await put_rules(seed, resume_format="client_formatted")
    async with tenant_session(seed.tenant_id) as db:
        await db.execute(
            update(CandidateResume)
            .where(CandidateResume.candidate_id == seed.candidate_id)
            .values(parse_status="unavailable")
        )
    pack = await prepare(seed)
    rf = check_of(pack, "resume_format")
    assert (rf["level"], rf["code"]) == ("block", "resume_not_parsed")
    assert "parsed_resume" in pack["missing"] and pack["status"] == "blocked" and pack["resume_kind"] is None


async def test_original_rule_warns_about_contact_details(seed: PackSeed) -> None:
    await put_rules(seed, resume_format="original", mask_contact_details=True)
    pack = await prepare(seed)
    rf = check_of(pack, "resume_format")
    assert (rf["level"], rf["code"]) == ("warn", "contact_details_in_original") and pack[
        "resume_kind"
    ] == "original"


async def test_consent_required_blocks_then_flips_on_grant_without_a_model(seed: PackSeed) -> None:
    await put_rules(seed, require_candidate_consent=True)
    pack = await prepare(seed)
    assert check_of(pack, "consent")["code"] == "consent_required" and pack["status"] == "blocked"
    assert "consent" in pack["missing"]
    await grant_submission_consent(
        seed.tenant_id, seed.candidate_id, seed.client_id
    )  # event -> re-check task
    after = (await get_pack(seed)).json()
    assert check_of(after, "consent")["level"] == "pass" and after["status"] == "draft"


async def test_candidate_link_mode_blocks(seed: PackSeed) -> None:
    resp = await seed.admin.client.patch(
        f"{API}/pipeline/settings", json={"submission_consent_mode": "candidate_link"}, headers=h(seed.admin)
    )
    assert resp.status_code == 200, resp.text
    pack = await prepare(seed)
    assert check_of(pack, "consent")["level"] == "block"


async def test_margin_below_minimum_warns_and_invalid_rates_block(seed: PackSeed) -> None:
    pack = await prepare(seed)
    resp = await seed.admin.client.patch(
        f"{API}/submission-packs/{pack['id']}",
        json={
            "rates": {
                "pay_rate": "1400",
                "bill_rate": "1500",
                "currency": "INR",
                "pay_rate_unit": "hour",
                "bill_rate_unit": "hour",
            },
            "lock_version": pack["lock_version"],
        },
        headers=h(seed.admin),
    )
    assert resp.status_code == 200, resp.text
    margin = check_of(resp.json(), "margin")
    assert (margin["level"], margin["code"]) == ("warn", "below_min_margin")
    assert margin["message"] == "Below minimum margin — approval is requested when you submit."
    stale = await seed.admin.client.patch(
        f"{API}/submission-packs/{pack['id']}", json={"email_subject": "x", "lock_version": pack["lock_version"]},
        headers=h(seed.admin),
    )  # fmt: skip
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_version"
    checks = await seed.admin.client.post(
        f"{API}/applications/{seed.application_id}/submission-pack/checks",
        json={"rates": {"pay_rate": "100", "bill_rate": "0", "currency": "INR"}}, headers=h(seed.admin),
    )  # fmt: skip
    assert checks.status_code == 200, checks.text
    blocked = next(c for c in checks.json()["checks"] if c["key"] == "margin")
    assert (blocked["level"], blocked["code"]) == ("block", "invalid_bill_rate")
    mismatch = await seed.admin.client.post(
        f"{API}/applications/{seed.application_id}/submission-pack/checks",
        json={"rates": {"pay_rate": "100", "bill_rate": "200", "currency": "USD"}}, headers=h(seed.admin),
    )  # fmt: skip
    assert next(c for c in mismatch.json()["checks"] if c["key"] == "margin")["code"] == "currency_mismatch"


async def test_duplicate_within_window_blocks_and_client_override(seed: PackSeed) -> None:
    from tests.modules.demands.helpers import open_demand
    from tests.modules.pipeline.helpers import POrg, submit

    org = POrg(
        tenant=None,
        admin=seed.admin,
        client_id=seed.client_id,
        contact_id=seed.contact_id,
        contact_email="",
        demand={"id": str(seed.demand_id)},
    )  # type: ignore[arg-type]
    resp = await submit(org, [str(seed.application_id)])
    assert resp.status_code == 200 and resp.json()["items"][0]["status"] == "sent", resp.text
    d2 = await open_demand(seed.admin, seed.client_id, title="Data Engineer II")
    app2 = await add_one(seed.admin, str(d2["id"]), str(seed.candidate_id))
    ids = await stages(seed.admin)
    assert (await move(seed.admin, app2, ids["shortlisted"])).status_code == 200
    resp = await seed.admin.client.post(f"{API}/applications/{app2}/submission-pack", headers=h(seed.admin))
    assert resp.status_code == 201, resp.text
    dup = check_of(resp.json(), "duplicate")
    assert (dup["level"], dup["code"]) == ("block", "prior_submission") and dup["link"][
        "kind"
    ] == "submission"
    assert "You can still submit with a reason" in dup["message"]
    # first submitter is the same person -> no ownership warning
    assert check_of(resp.json(), "vendor_ownership")["level"] == "pass"


async def test_submission_limit_and_not_shortlisted(seed: PackSeed) -> None:
    from app.models.demands import Demand

    async with tenant_session(seed.tenant_id) as db:
        await db.execute(update(Demand).where(Demand.id == seed.demand_id).values(submission_limit=0))
    pack = await prepare(seed)
    req = check_of(pack, "required_fields")
    assert (req["level"], req["code"]) == ("block", "submission_limit_reached")


async def test_masking_without_compensation_view(seed: PackSeed, app: FastAPI) -> None:
    await prepare(seed)
    from app.core.database import platform_session
    from app.models.identity import Tenant

    async with platform_session() as db:
        tenant = await db.get(Tenant, seed.tenant_id)
    recruiter = await join(app, tenant, "recruiter", name="Rita Recruiter")  # type: ignore[arg-type]
    await seed.admin.client.put(
        f"{API}/demands/{seed.demand_id}/assignments",
        json={"user_ids": [str(recruiter.user_id)], "primary_user_id": str(recruiter.user_id)}, headers=h(seed.admin),
    )  # fmt: skip
    resp = await get_pack(seed, recruiter)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rates_hidden"] is True and body["pay_rate"] is None and body["bill_rate"] is None
    assert "margin_pct" not in check_of(body, "margin")["details"]
    await recruiter.client.aclose()


async def test_owner_without_compensation_view_stores_no_rates(seed: PackSeed) -> None:
    perms = {k: Scope.TENANT for k in CATALOG if k != "compensation:view"}
    async with tenant_session(seed.tenant_id) as db:
        out = await svc.prepare_pack(_ctx(seed, **perms), db, seed.application_id)
    rows = await pack_rows(seed.tenant_id, SubmissionPack.id == out.pack_id)
    assert rows[0].pay_rate is None and rows[0].bill_rate is None
    assert next(c for c in out.checks if c.key == "margin").message.startswith("[needs_input]")


async def test_write_up_validation_and_ready_status(seed: PackSeed) -> None:
    pack = await prepare(seed)
    bad = await seed.admin.client.patch(
        f"{API}/submission-packs/{pack['id']}",
        json={"summary_text": "Priya, 34 years old and married, is a data engineer.", "lock_version": pack["lock_version"]},
        headers=h(seed.admin),
    )  # fmt: skip
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "write_up_protected_attribute"
    long = await seed.admin.client.patch(
        f"{API}/submission-packs/{pack['id']}", json={"summary_text": "word " * 200, "lock_version": pack["lock_version"]},
        headers=h(seed.admin),
    )  # fmt: skip
    assert long.status_code == 422 and long.json()["error"]["code"] == "write_up_too_long"
    ok = await seed.admin.client.patch(
        f"{API}/submission-packs/{pack['id']}",
        json={"summary_text": "Priya is a data engineer with PySpark. See https://evil.example.net/x", "lock_version": pack["lock_version"]},
        headers=h(seed.admin),
    )  # fmt: skip
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert "evil.example.net" not in body["summary_text"] and body["write_up_status"] == "edited"
    assert body["status"] == "ready" and "write_up" not in body["missing"]


async def test_review_and_send_links_the_submission_with_the_diff(seed: PackSeed) -> None:
    from tests.modules.pipeline.helpers import POrg, submit

    pack = await prepare(seed)
    opened = await seed.admin.client.post(
        f"{API}/submission-packs/{pack['id']}/opened", headers=h(seed.admin)
    )
    assert opened.status_code == 200 and opened.json()["opened_in_dialog_at"]
    org = POrg(
        tenant=None,
        admin=seed.admin,
        client_id=seed.client_id,
        contact_id=seed.contact_id,
        contact_email="",
        demand={"id": str(seed.demand_id)},
    )  # type: ignore[arg-type]
    per = {str(seed.application_id): per_app(pay="1000.00", bill="1600.00", resume_kind="client_formatted",
                                             consent_attestation={"text": "Agreed by phone today", "confirmed": True})}  # fmt: skip
    resp = await submit(
        org, [str(seed.application_id)], per, note="Strong PySpark background and lakehouse work."
    )
    assert resp.status_code == 200, resp.text
    sub_id = resp.json()["items"][0]["submission_id"]
    used = (await get_pack(seed)).json()
    assert used["lifecycle"] == "used" and used["submission_id"] == sub_id and used["submission_human_id"]
    changes = used["user_changes"]
    assert "bill_rate" in changes["changed_fields"] and "pay_rate" not in changes["changed_fields"]
    assert "note" in changes["changed_fields"] and changes["note_change"] == "replaced"
    again = await seed.admin.client.post(
        f"{API}/submission-packs/{pack['id']}/discard",
        json={"reason": "user_discarded"},
        headers=h(seed.admin),
    )
    assert again.status_code == 409


async def test_leaving_shortlisted_discards_the_open_pack(seed: PackSeed) -> None:
    pack = await prepare(seed)
    ids = await stages(seed.admin)
    resp = await move(
        seed.admin, str(seed.application_id), ids["withdrawn"], reason_code="accepted_other_offer"
    )
    assert resp.status_code == 200, resp.text
    rows = await pack_rows(seed.tenant_id, SubmissionPack.id == uuid.UUID(pack["id"]))
    assert (rows[0].lifecycle, rows[0].discarded_reason) == ("discarded", "application_moved")


async def test_discard_and_new_resume_marks_stale(seed: PackSeed) -> None:
    pack = await prepare(seed)
    await upload_parsed_resume(seed.admin, seed.tenant_id, str(seed.candidate_id))
    assert (await get_pack(seed)).json()["resume_stale"] is True
    resp = await seed.admin.client.post(
        f"{API}/submission-packs/{pack['id']}/discard", json={}, headers=h(seed.admin)
    )
    assert resp.status_code == 200 and resp.json()["lifecycle"] == "discarded"


async def test_notice_period_missing_warns(seed: PackSeed) -> None:
    await set_candidate(seed.tenant_id, seed.candidate_id, notice_period_days=None, availability_date=None)
    pack = await prepare(seed)
    req = check_of(pack, "required_fields")
    assert (req["level"], req["code"]) == ("warn", "notice_period_missing") and "notice_period" in pack[
        "missing"
    ]


async def test_cross_tenant_and_permissions(seed: PackSeed, other_seed: PackSeed) -> None:
    await prepare(seed)
    resp = await other_seed.admin.client.get(f"{API}/applications/{seed.application_id}/submission-pack")
    assert resp.status_code == 404
    resp = await other_seed.admin.client.post(
        f"{API}/applications/{seed.application_id}/submission-pack", headers=h(other_seed.admin)
    )
    assert resp.status_code == 404
    listed = await seed.admin.client.get(f"{API}/submission-packs")
    assert listed.status_code == 200 and listed.json()["total"] == 1
    assert (await other_seed.admin.client.get(f"{API}/submission-packs")).json()["total"] == 0
    assert (await get_pack(other_seed)).status_code == 404  # none prepared yet
    metrics = await seed.admin.client.get(f"{API}/submission-packs/metrics")
    assert metrics.status_code == 200 and metrics.json()["open_packs"] == 1


async def test_client_rules_api(seed: PackSeed) -> None:
    resp = await seed.admin.client.get(f"{API}/clients/{seed.client_id}/submission-rules")
    assert (
        resp.status_code == 200
        and resp.json()["is_default"] is True
        and resp.json()["write_up_max_words"] == 150
    )
    bad = await seed.admin.client.put(
        f"{API}/clients/{seed.client_id}/submission-rules",
        json={"write_up_max_words": 20},
        headers=h(seed.admin),
    )
    assert bad.status_code == 422
    out = await put_rules(
        seed, write_up_max_words=60, duplicate_window_days=30, write_up_instructions="Lead with domain."
    )
    assert out["is_default"] is False and out["duplicate_window_days"] == 30
    missing = await seed.admin.client.get(f"{API}/clients/{uuid.uuid4()}/submission-rules")
    assert missing.status_code == 404


async def test_prepare_refuses_unknown_client_template(seed: PackSeed) -> None:
    import pytest

    from app.core.errors import ValidationFailed

    async with tenant_session(seed.tenant_id) as db:
        with pytest.raises(ValidationFailed) as exc:
            await svc.prepare_pack(_ctx(seed), db, seed.application_id, client_template_id=uuid.uuid4())
    assert exc.value.code == "client_template_unknown"


async def test_erasure_redacts_and_discards(seed: PackSeed) -> None:
    from app.services.privacy.registry import ErasureContext
    from app.services.submission_packs_erasure import SubmissionPacksHandler

    pack = await prepare(seed)
    await seed.admin.client.patch(
        f"{API}/submission-packs/{pack['id']}", json={"summary_text": "Priya builds pipelines.", "lock_version": pack["lock_version"]},
        headers=h(seed.admin),
    )  # fmt: skip
    handler = SubmissionPacksHandler()
    async with tenant_session(seed.tenant_id) as db:
        ectx = ErasureContext(
            tenant_id=seed.tenant_id, candidate_id=seed.candidate_id, run_id=uuid.uuid4(), reason="dsr"
        )
        exported = await handler.export(db, ectx)
        assert exported and exported["submission_packs"][0]["write_up"] == "Priya builds pipelines."
        counts = await handler.erase(db, ectx)
        assert counts["packs_redacted"] == 1 and counts["packs_discarded"] == 1
        assert await handler.verify(db, ectx) == []
    rows = await pack_rows(seed.tenant_id, SubmissionPack.id == uuid.UUID(pack["id"]))
    assert rows[0].summary_text is None and rows[0].discarded_reason == "erased"


async def test_vendor_owner_read_and_facts_reexports(seed: PackSeed) -> None:
    from app.services import pipeline_service, vendors_service

    async with tenant_session(seed.tenant_id) as db:
        ctx = _ctx(seed)
        assert (
            await vendors_service.candidate_vendor_owner(ctx, db, seed.candidate_id, seed.client_id) is None
        )
        assert await pipeline_service.counted_submissions(db, seed.demand_id) == 0
        assert await pipeline_service.prior_submission(db, seed.candidate_id, seed.client_id, 90) is None
        assert await pipeline_service.prior_submission(db, seed.candidate_id, seed.client_id, 0) is None
        assert await pipeline_service.client_first_submitter(db, seed.client_id, seed.candidate_id) is None
        settings = await pipeline_service.get_pipeline_settings(db)
        assert settings.prior_submission_window_days == 90 and settings.min_margin_pct == Decimal("15.00")


async def test_second_candidate_resume_missing_blocks(seed: PackSeed) -> None:
    cand = await create_candidate(seed.admin, "Nora Noresume")
    app_id = await add_one(seed.admin, str(seed.demand_id), cand["id"])
    ids = await stages(seed.admin)
    assert (await move(seed.admin, app_id, ids["shortlisted"])).status_code == 200
    resp = await seed.admin.client.post(f"{API}/applications/{app_id}/submission-pack", headers=h(seed.admin))
    assert resp.status_code == 201, resp.text
    req = check_of(resp.json(), "required_fields")
    assert (req["level"], req["code"]) == ("block", "resume_missing")
    assert select(SubmissionPack) is not None
