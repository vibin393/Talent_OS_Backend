"""Epic 18 FR-103: compliance packs (download audit / log), home tiles, team and PO routes."""

import uuid

from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.client_portal import ClientPortalDownload
from tests.epic3 import resume_pdf
from tests.modules.client_portal.helpers import CP, client_user, other_client
from tests.modules.pipeline.helpers import POrg, h


async def _pack(porg: POrg, client_id: uuid.UUID, publish: bool = True) -> tuple[str, str]:
    m = porg.admin
    made = await m.client.post(
        f"/api/v1/clients/{client_id}/compliance-packs",
        json={"period_month": "2026-09-01", "title": "September statutory pack"},
        headers=h(m),
    )
    assert made.status_code == 201, made.text
    pack_id = made.json()["id"]
    item = await m.client.post(
        f"/api/v1/clients/{client_id}/compliance-packs/{pack_id}/items",
        files={"file": ("pf-challan.pdf", resume_pdf("PF challan September"), "application/pdf")},
        data={"label": "PF challan"},
        headers=h(m),
    )
    assert item.status_code == 200, item.text
    item_id = item.json()["items"][0]["id"]
    if publish:
        pub = await m.client.post(
            f"/api/v1/clients/{client_id}/compliance-packs/{pack_id}/publish", headers=h(m)
        )
        assert pub.status_code == 200, pub.text
    return pack_id, item_id


async def test_compliance_pack_download_is_audited_and_logged(app: FastAPI, porg: POrg) -> None:
    cu = await client_user(app, porg.tenant_id, porg.client_id)
    hidden_pack, hidden_item = await _pack(porg, porg.client_id, publish=False)
    assert (await cu.client.get(f"{CP}/compliance-packs")).json() == []
    assert (await cu.client.get(f"{CP}/compliance-packs/items/{hidden_item}/download")).status_code == 404

    pack_id, item_id = await _pack(porg, porg.client_id)
    packs = (await cu.client.get(f"{CP}/compliance-packs")).json()
    assert [p["id"] for p in packs] == [pack_id] and packs[0]["items"][0]["label"] == "PF challan"
    dl = await cu.client.get(f"{CP}/compliance-packs/items/{item_id}/download")
    assert dl.status_code == 200 and dl.content[:4] == b"%PDF"
    async with tenant_session(porg.tenant_id) as db:
        rows = (await db.execute(select(ClientPortalDownload))).scalars().all()
        assert len(rows) == 1 and rows[0].client_user_id == cu.id
        from app.core.audit import AuditLog

        audits = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "compliance_pack.downloaded")))
            .scalars()
            .all()
        )
        assert len(audits) == 1
    log = (await porg.admin.client.get(f"/api/v1/clients/{porg.client_id}/compliance-packs")).json()
    assert any(len(p["downloads"]) == 1 for p in log if p["id"] == pack_id)

    # another client's user sees nothing
    other_id = await other_client(porg)
    stranger = await client_user(app, porg.tenant_id, other_id)
    assert (await stranger.client.get(f"{CP}/compliance-packs/{pack_id}")).status_code == 404
    assert (await stranger.client.get(f"{CP}/compliance-packs/items/{item_id}/download")).status_code == 404
    assert hidden_pack != pack_id


async def test_cross_tenant_internal_pack_is_404(app: FastAPI, porg: POrg, other_porg: POrg) -> None:
    resp = await other_porg.admin.client.get(f"/api/v1/clients/{porg.client_id}/compliance-packs")
    assert resp.status_code == 404


async def test_home_tiles_follow_permissions(app: FastAPI, porg: POrg) -> None:
    full = await client_user(app, porg.tenant_id, porg.client_id)
    home = (await full.client.get(f"{CP}/home")).json()
    assert home["approvals_pending"] == 0 and home["team"] == {"size": 0, "ending_within_30_days": 0}
    assert home["new_submissions"] == 0
    bare = await client_user(app, porg.tenant_id, porg.client_id, permissions=())
    home2 = (await bare.client.get(f"{CP}/home")).json()
    assert home2["team"] is None and home2["new_submissions"] is None and home2["invoices_issued"] is None
    assert (await bare.client.get(f"{CP}/team")).status_code == 403
    assert (await bare.client.get(f"{CP}/purchase-orders")).status_code == 403
    assert (await full.client.get(f"{CP}/team")).json() == []
    assert (await full.client.get(f"{CP}/purchase-orders")).status_code == 200
