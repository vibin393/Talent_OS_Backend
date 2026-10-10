# ruff: noqa: E501
"""Epic 69 (story 69.5, spec A7 / A8): partner submission -> automated checks -> reviewer checklist -> ★ approve ->
published; suspension in every Tenant; the conformance report verification; partner-visible messages only; the
reviewer role and the human-only operator guard."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.core import net_safety
from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.developer import DeveloperContext
from app.core.errors import ConflictError, Forbidden
from app.core.permissions import Principal
from app.models.identity import PlatformOperator
from app.models.oauth import App, AppInstallation, PartnerOrg
from app.schemas.marketplace import ListingDraftIn, VersionDraftIn
from app.schemas.platform_marketplace import CHECKLIST_KEYS, ChecklistIn, ChecklistItemIn
from app.services import marketplace_service, vault_service
from app.services.marketplace import checks, conformance_report, submissions
from app.services.marketplace_service import review
from tests.epic3 import Org3
from tests.support.oauth import install_app

KEY = "test-conformance-signing-key"
GOOD_REASON = "The app reads candidates to sync them into the partner ATS."


async def _signing_key() -> None:
    await vault_service.platform_put(conformance_report.SIGNING_SECRET, KEY)


def _report(
    app_id: uuid.UUID, sha: str, *, finished: datetime | None = None, passed: bool = True
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": conformance_report.SCHEMA, "kit_version": "1.0", "app_id": str(app_id), "manifest_sha256": sha,
        "sandbox_tenant_ref": "sbx-1", "started_at": (finished or datetime.now(UTC)).isoformat(),
        "finished_at": (finished or datetime.now(UTC)).isoformat(), "passed": passed,
        "checks": [{"key": k, "passed": True, "detail": "ok"} for k in conformance_report.REQUIRED_CHECKS],
        "observed_scopes": ["candidates:read"], "observed_hosts": ["api.partner.example"],
    }  # fmt: skip
    report["signature"] = conformance_report.sign(report, KEY)
    return report


async def _partner() -> tuple[DeveloperContext, App]:
    org = PartnerOrg(
        id=uuid.uuid4(),
        name="Acme Partners",
        slug=f"acme-{secrets.token_hex(3)}",
        verification_status="verified",
    )
    async with platform_session() as pdb:
        pdb.add(org)
    slug = f"acme-sync-{secrets.token_hex(4)}"
    app = App(
        id=uuid.uuid4(), client_id=f"tosapp_{secrets.token_hex(13)}", name="Acme ATS Sync", slug=slug, publisher_kind="partner",
        partner_org_id=org.id, client_type="confidential", grant_types=["client_credentials"], redirect_uris=["https://partner.example/cb"],
        resources=["api"], requested_scopes=["candidates:read"], scope_reasons={"candidates:read": GOOD_REASON},
        manifest={"name": "Acme ATS Sync", "data_flows": [{"direction": "out", "entity": "candidate", "fields": ["name"], "purpose": "Sync", "destination_country": "IN"}]},
        manifest_sha256=hashlib.sha256(slug.encode()).hexdigest(), status="development", created_by_kind="developer",
    )  # fmt: skip
    async with platform_session() as pdb:
        pdb.add(app)
    dctx = DeveloperContext(
        developer_id=uuid.uuid4(), partner_org_id=org.id, role="owner", partner_verified=True
    )
    return dctx, app


async def _operator(roles: list[str]) -> RequestContext:
    op = PlatformOperator(
        id=uuid.uuid4(),
        email=f"op-{secrets.token_hex(4)}@talentos.test",
        name="Op",
        password_hash="x",
        roles=roles,
    )
    async with platform_session() as pdb:
        pdb.add(op)
    return RequestContext(principal=Principal(type="platform", id=op.id))


@pytest.fixture(autouse=True)
def _no_kicker() -> Any:
    """Run the automated checks explicitly (with the stub transport) instead of the eager Celery kick."""
    kicker = submissions._kicker
    submissions.set_checks_kicker(None)
    yield
    submissions.set_checks_kicker(kicker)


@pytest.fixture
def _net() -> Any:
    async def resolver(host: str) -> list[str]:
        return ["93.184.216.34"]

    net_safety.set_resolver(resolver)
    yield httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))
    net_safety.set_resolver(None)


async def _submit(
    dctx: DeveloperContext, app: App, report: dict[str, Any] | None, *, slug: str | None = None
) -> uuid.UUID:
    listing = await marketplace_service.create_listing_draft(dctx, app_id=app.id, data=ListingDraftIn(
        slug=slug or f"acme-{secrets.token_hex(3)}", name="Acme ATS Sync", tagline="Sync candidates with Acme ATS",
        category="other", support_url="https://partner.example/support",
    ))  # fmt: skip
    v = await marketplace_service.save_version_draft(dctx, listing.id, VersionDraftIn(
        description_md="Keeps Acme ATS in step with Talent OS.", privacy_url="https://partner.example/privacy",
        dpa_url="https://partner.example/dpa", changelog="First version",
    ))  # fmt: skip
    await marketplace_service.submit_version(
        dctx, v.id, conformance_report=report if report is not None else {}
    )
    return v.id


async def test_submission_checks_review_publish_and_suspend(_net: Any, org3: Org3, other3: Org3) -> None:
    await _signing_key()
    dctx, app = await _partner()
    vid = await _submit(dctx, app, _report(app.id, app.manifest_sha256))
    results = await submissions.run_checks(vid, transport=_net)
    assert results and all(r["passed"] for r in results), results
    reviewer = await _operator(["marketplace_reviewer"])
    out = await review.claim(reviewer, vid)
    assert out.status == "in_review"
    with pytest.raises(ConflictError) as exc:
        await review.approve(reviewer, vid, message="Looks good")
    assert exc.value.code == "checklist_incomplete"
    await review.save_checklist(
        reviewer,
        vid,
        ChecklistIn(items={k: ChecklistItemIn(ok=True) for k in CHECKLIST_KEYS}, internal_note="secret note"),
    )
    done = await review.approve(reviewer, vid, message="Welcome aboard")
    assert done.status == "published" and done.listing_status == "published"
    async with platform_session() as pdb:
        assert (await pdb.get(App, app.id)).status == "published"  # type: ignore[union-attr]
    # the partner sees messages, never the internal note
    versions = await marketplace_service.partner_versions(dctx, dctx.partner_org_id)  # type: ignore[arg-type]
    assert "Welcome aboard" in versions[0].messages and "secret note" not in versions[0].model_dump_json()
    # suspension reaches every tenant
    i1 = await install_app(org3.tenant_id, app, granted_by=org3.admin.user_id)
    i2 = await install_app(other3.tenant_id, app, granted_by=other3.admin.user_id)
    await review.suspend_listing(reviewer, done.listing_id, reason="Security incident")
    for org, inst in ((org3, i1), (other3, i2)):
        async with tenant_session(org.tenant_id) as db:
            row = await db.get(AppInstallation, inst.id)
            assert row is not None and row.status == "suspended"
    await review.reinstate_listing(reviewer, done.listing_id, reason="Fixed")
    async with tenant_session(org3.tenant_id) as db:
        row = await db.get(AppInstallation, i1.id)
        assert row is not None and row.status == "active"


async def test_missing_report_and_bad_reports(_net: Any) -> None:
    await _signing_key()
    dctx, app = await _partner()
    vid = await _submit(dctx, app, None)
    results = {r["key"]: r for r in await submissions.run_checks(vid, transport=_net)}
    assert results["conformance_report"]["detail"] == "conformance_report_missing"
    async with platform_session() as pdb:
        from app.models.marketplace import MarketplaceListingVersion

        assert (await pdb.get(MarketplaceListingVersion, vid)).status == "changes_requested"  # type: ignore[union-attr]
    bad = _report(app.id, app.manifest_sha256)
    bad["passed"] = True
    bad["signature"] = "v1=" + "0" * 64
    assert "signature_invalid" in await conformance_report.verify(
        bad, app_id=app.id, manifest_sha256=app.manifest_sha256
    )
    old = _report(app.id, app.manifest_sha256, finished=datetime.now(UTC) - timedelta(days=31))
    assert "expired" in await conformance_report.verify(
        old, app_id=app.id, manifest_sha256=app.manifest_sha256
    )
    assert (
        await conformance_report.verify(
            _report(app.id, app.manifest_sha256), app_id=app.id, manifest_sha256=app.manifest_sha256
        )
        == []
    )


async def test_url_check_refuses_private_addresses() -> None:
    async def resolver(host: str) -> list[str]:
        return ["10.0.0.5"]

    net_safety.set_resolver(resolver)
    try:
        result = await checks.check_urls_reachable(
            {"privacy_url": "https://partner.example/privacy", "dpa_url": "https://partner.example/dpa"}
        )
    finally:
        net_safety.set_resolver(None)
    assert not result.passed and "refused" in (result.detail or "")


def test_listing_text_and_transfer_checks() -> None:
    assert not checks.check_listing_text("Acme", "We auto-reject weak candidates", "").passed
    assert checks.check_listing_text("Acme", "Sync candidates", "Plain description").passed
    assert not checks.check_transfer_statement([{"destination_country": "US"}], None).passed
    assert checks.check_transfer_statement([{"destination_country": "US"}], "SCCs signed").passed


async def test_role_and_human_operator_required() -> None:
    dctx, app = await _partner()
    vid = await _submit(dctx, app, None)
    no_role = await _operator([])
    with pytest.raises(Forbidden) as exc:
        await review.claim(no_role, vid)
    assert exc.value.code == "platform_role_required"
    automated = RequestContext(
        actor_kind="system", principal=(await _operator(["marketplace_reviewer"])).principal
    )
    with pytest.raises(Forbidden) as exc2:
        await review.reject(automated, vid, message="no")
    assert exc2.value.code == "human_only_action"
    async with platform_session() as pdb:
        ops = (
            await pdb.execute(
                select(PlatformOperator.roles).where(PlatformOperator.id == no_role.principal.id)
            )
        ).scalar_one()  # type: ignore[union-attr]
    assert ops == []
