"""Story 22.9 (A10): ``comms.emails`` (52), ``notifications.texts`` (57) and ``tasks.texts`` (58) erase the
candidate's logged emails, notification texts and task texts, honour ``ctx.retained`` and verify clean."""

import uuid

from fastapi import FastAPI
from sqlalchemy import func, select

from app.core.database import tenant_session
from app.models.comms import Email, EmailRecipient
from app.models.notifications import Notification, NotificationDelivery
from app.models.tasks import Task
from app.services.comms_erasure import ERASED_DOMAIN, CommsEmailsHandler
from app.services.notifications_erasure import NotificationTextsHandler
from app.services.privacy import registry
from app.services.privacy.registry import ErasureContext
from app.services.tasks_erasure import TaskTextsHandler
from tests.modules.identity.conftest import join, make_tenant

ERASED = "[erased]"


def test_handlers_are_registered_with_spec_orders() -> None:
    by_name = {h.name: h for h in registry.handlers()}
    assert by_name["comms.emails"].order == 52
    assert by_name["notifications.texts"].order == 57
    assert by_name["tasks.texts"].order == 58
    claimed = registry.claimed_tables()
    for table in ("email_messages", "email_recipients", "notifications", "notification_deliveries", "tasks"):
        assert table in claimed


async def test_erase_honours_retained_and_verifies_clean(app: FastAPI) -> None:
    tenant = await make_tenant()
    admin = await join(app, tenant)
    created = await admin.client.post(
        "/api/v1/candidates", json={"full_name": "Erin Erasable"}, headers=admin.h()
    )
    assert created.status_code == 201, created.text
    cand = uuid.UUID(created.json()["id"])
    tid, uid = tenant.id, admin.user_id
    ref = [{"entity_type": "candidate", "entity_id": str(cand)}]
    async with tenant_session(tid) as db:
        emails = [
            Email(
                id=uuid.uuid4(),
                tenant_id=tid,
                from_address="r@agency.example.com",
                subject=f"Interview for Erin {i}",
                body_html="<p>Hi Erin</p>",
                body_text="Hi Erin",
                related=ref,
            )
            for i in range(2)
        ]
        db.add_all(emails)
        await db.flush()
        db.add_all(
            [
                EmailRecipient(
                    id=uuid.uuid4(),
                    tenant_id=tid,
                    message_id=e.id,
                    address="erin@example.com",
                    entity_type="candidate",
                    entity_id=cand,
                )
                for e in emails
            ]
        )
        staff = Notification(
            id=uuid.uuid4(), tenant_id=tid, user_id=uid, recipient_kind="user", recipient_id=uid,
            type="x.test", title="Erin moved to interview", body="Erin Erasable", entity_type="candidate",
            entity_id=cand, dedupe_key=uuid.uuid4().hex,
        )  # fmt: skip
        portal = Notification(
            id=uuid.uuid4(), tenant_id=tid, user_id=None, recipient_kind="candidate", recipient_id=cand,
            candidate_id=cand, type="x.test", title="Your interview, Erin", dedupe_key=uuid.uuid4().hex,
        )  # fmt: skip
        db.add_all([staff, portal])
        await db.flush()
        db.add(
            NotificationDelivery(id=uuid.uuid4(), tenant_id=tid, notification_id=portal.id, channel="email")
        )
        tasks = [
            Task(
                id=uuid.uuid4(),
                tenant_id=tid,
                title=f"Call Erin {i}",
                notes="Erin's phone",
                owner_id=uid,
                created_by=uid,
                entity_type="candidate",
                entity_id=cand,
            )
            for i in range(2)
        ]
        db.add_all(tasks)
        await db.flush()
        kept_email, kept_task = emails[1].id, tasks[1].id
        email_ids, task_ids, staff_id = [e.id for e in emails], [t.id for t in tasks], staff.id

    retained = {"email_messages": {kept_email}, "tasks": {kept_task}}
    ctx = ErasureContext(tid, cand, uuid.uuid4(), "dsr", retained=retained)
    handlers = (CommsEmailsHandler(), NotificationTextsHandler(), TaskTextsHandler())
    async with tenant_session(tid) as db:
        assert (await handlers[0].export(db, ctx))["emails"]  # type: ignore[index]
        counts = [await h.erase(db, ctx) for h in handlers]
    assert counts[0] == {"email_messages": 1, "email_recipients": 1}
    assert counts[1] == {"notifications": 2, "notification_deliveries": 1}
    assert counts[2] == {"tasks": 1}
    async with tenant_session(tid) as db:
        assert [await h.verify(db, ctx) for h in handlers] == [[], [], []]
        by_id = {e.id: e for e in (await db.execute(select(Email).where(Email.id.in_(email_ids)))).scalars()}
        assert by_id[email_ids[0]].subject == ERASED and by_id[email_ids[0]].related == []
        assert by_id[kept_email].subject == "Interview for Erin 1"  # retained
        addresses = {
            r.message_id: r.address
            for r in (
                await db.execute(select(EmailRecipient).where(EmailRecipient.message_id.in_(email_ids)))
            ).scalars()
        }
        assert addresses[email_ids[0]].endswith(ERASED_DOMAIN) and addresses[kept_email] == "erin@example.com"
        assert (await db.get(Notification, staff_id)).title == ERASED  # type: ignore[union-attr]
        addressed = select(func.count()).select_from(Notification)
        addressed = addressed.where(
            Notification.recipient_kind == "candidate", Notification.recipient_id == cand
        )
        left = (await db.execute(addressed)).scalar_one()
        assert left == 0
        titles = {
            t.id: t.title for t in (await db.execute(select(Task).where(Task.id.in_(task_ids)))).scalars()
        }
        assert titles[task_ids[0]] == ERASED and titles[kept_task] == "Call Erin 1"
        # A second run is idempotent.
        assert (await handlers[2].erase(db, ctx)) == {"tasks": 1}
