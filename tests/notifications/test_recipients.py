"""AD-31 notification recipients beyond users: client_user / vendor_user / candidate (Epic 13, story 13.7)."""

import uuid

from sqlalchemy import func, select

from app.core.context import RequestContext
from app.core.database import OrgScope, tenant_session
from app.core.pagination import PageParams
from app.core.permissions import Principal
from app.models.notifications import Notification
from app.services import notifications_service as ns
from app.services.notifications_service import Content, Recipient
from tests.epic3 import create_candidate
from tests.modules.demands.helpers import DOrg, create_client
from tests.p2 import make_client_user, make_vendor, make_vendor_user


async def _deliver(
    tenant_id: uuid.UUID, recipients: list[uuid.UUID | Recipient], title: str
) -> list[uuid.UUID]:
    async with tenant_session(tenant_id) as db:
        return await ns.deliver(
            db, "workflow_notice", recipients, Content(title=title), event_id=uuid.uuid4().hex
        )


def _ctx(tenant_id: uuid.UUID, kind: str, pid: uuid.UUID, org_id: uuid.UUID | None = None) -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id, principal=Principal(type=kind, id=pid, tenant_id=tenant_id, org_id=org_id)
    )  # type: ignore[arg-type]


async def test_external_recipients_and_org_isolation(dorg: DOrg) -> None:
    tid = dorg.tenant_id
    other_client = await create_client(dorg.admin)
    cu_a = await make_client_user(tid, dorg.client_id)
    cu_b = await make_client_user(tid, other_client)
    vendor = await make_vendor(tid)
    vu = await make_vendor_user(tid, vendor)
    cand = await create_candidate(
        dorg.admin, full_name="Wanda Worker", emails=[{"email": "wanda@example.com"}]
    )
    cand_id = uuid.UUID(cand["id"])

    users = await _deliver(
        tid,
        [
            Recipient("client_user", cu_a),
            Recipient("client_user", cu_b),
            Recipient("vendor_user", vu),
            Recipient("candidate", cand_id),
            Recipient("client_user", uuid.uuid4()),  # unknown: skipped silently
            dorg.admin.user_id,
        ],
        "Timesheet due",
    )
    assert users == [dorg.admin.user_id]

    async with tenant_session(tid) as db:
        rows = (
            (await db.execute(select(Notification).where(Notification.title == "Timesheet due")))
            .scalars()
            .all()
        )
        by_kind = {(r.recipient_kind, r.recipient_id): r for r in rows}
        assert set(by_kind) == {
            ("client_user", cu_a),
            ("client_user", cu_b),
            ("vendor_user", vu),
            ("candidate", cand_id),
            ("user", dorg.admin.user_id),
        }
        assert (
            by_kind["client_user", cu_a].client_id == dorg.client_id
            and by_kind["client_user", cu_a].user_id is None
        )
        assert by_kind["vendor_user", vu].vendor_id == vendor
        assert by_kind["candidate", cand_id].candidate_id == cand_id
        assert by_kind["user", dorg.admin.user_id].user_id == dorg.admin.user_id

    # Org RLS: a client session sees only its client's rows; vendor and worker likewise.
    async with tenant_session(tid, org=OrgScope.of("client", dorg.client_id)) as db:
        seen = (await db.execute(select(Notification.recipient_id))).scalars().all()
        assert seen == [cu_a]
        page = await ns.list_for_principal(_ctx(tid, "client_user", cu_a, dorg.client_id), db, PageParams())
        assert [n.title for n in page.items] == ["Timesheet due"]
        # another Client User of the same client sees nothing of cu_a's
        assert (await ns.unread_count(_ctx(tid, "client_user", uuid.uuid4(), dorg.client_id), db)).count == 0
    async with tenant_session(tid, org=OrgScope.of("vendor", vendor)) as db:
        assert (await db.execute(select(Notification.recipient_id))).scalars().all() == [vu]
    async with tenant_session(tid, org=OrgScope.of("worker", cand_id)) as db:
        assert (await db.execute(select(Notification.recipient_id))).scalars().all() == [cand_id]
        page = await ns.list_notifications(_ctx(tid, "candidate", cand_id), db, False, PageParams())
        assert page.total == 1
        await ns.mark_all_read(_ctx(tid, "candidate", cand_id), db)
        assert (await ns.unread_count(_ctx(tid, "candidate", cand_id), db)).count == 0


async def test_p1_user_lists_unchanged(dorg: DOrg) -> None:
    tid = dorg.tenant_id
    cu = await make_client_user(tid, dorg.client_id)
    await _deliver(tid, [dorg.admin.user_id, Recipient("client_user", cu)], "Hello both")
    resp = await dorg.admin.client.get("/api/v1/notifications")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["title"] for i in items if i["title"] == "Hello both"] == ["Hello both"]
    count = await dorg.admin.client.get("/api/v1/notifications/unread-count")
    assert count.status_code == 200 and count.json()["count"] >= 1
    async with tenant_session(tid) as db:
        total = (
            await db.execute(
                select(func.count()).select_from(Notification).where(Notification.title == "Hello both")
            )
        ).scalar_one()
        assert total == 2
