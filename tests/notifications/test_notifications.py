"""Notifications (story 10.3, FR-57): registry fan-out, idempotency, preferences, reads, digest, purge."""

import dataclasses
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import update

from app.core.database import tenant_session
from app.events.activity import UserMentioned
from app.models.notifications import Notification, NotificationPreference, NotificationSettings
from app.services import notifications_service
from tests.epic10 import API, NOTIFICATIONS, PREFS, TASKS, count, h, rows
from tests.modules.demands.helpers import DOrg, _org, open_demand
from tests.modules.identity.conftest import Member, join


@dataclasses.dataclass
class NOrg:
    org: DOrg
    a: Member
    b: Member
    demand_id: uuid.UUID

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.org.tenant_id


@pytest.fixture(scope="module")
async def norg(app: FastAPI) -> AsyncIterator[NOrg]:
    org = await _org(app, "Ada Admin")
    a = await join(app, org.tenant, name="Anu Alpha")
    b = await join(app, org.tenant, name="Ben Beta")
    demand = await open_demand(org.admin, org.client_id)
    yield NOrg(org=org, a=a, b=b, demand_id=uuid.UUID(demand["id"]))
    for m in (org.admin, a, b):
        await m.client.aclose()


def _payload(n: NOrg, type_key: str) -> tuple[dict[str, Any], set[uuid.UUID]]:
    """A synthetic event payload for ``type_key`` and the users expected to be notified (actor = admin)."""  # noqa: D401
    admin, a, b = n.org.admin.user_id, n.a.user_id, n.b.user_id
    s = str
    demand = s(n.demand_id)
    table: dict[str, tuple[dict[str, Any], set[uuid.UUID]]] = {
        "demand_assigned": ({"demand_id": demand, "added_user_ids": [s(a)], "expanded_team_member_ids": [s(b), s(admin)]}, {a, b}),  # noqa: E501
        "approval_requested": ({"request_id": s(uuid.uuid4()), "subject_type": "demand", "subject_id": demand, "approver_user_ids": [s(a), s(admin)], "approver_role_id": None}, {a}),  # noqa: E501
        "approval_decided": ({"request_id": s(uuid.uuid4()), "subject_type": "demand", "subject_id": demand, "status": "approved"}, {a}),  # noqa: E501
        "mention": ({"mentioned_user_id": s(a), "author_id": s(admin), "entity_type": "demand", "entity_id": demand}, {a}),  # noqa: E501
        "sla_at_risk": ({"demand_id": demand, "milestone": "assignment", "status": "at_risk", "recipient_ids": [s(a), s(b), s(admin)]}, {a, b}),  # noqa: E501
        "sla_breached": ({"demand_id": demand, "milestone": "assignment", "status": "breached", "recipient_ids": [s(b)]}, {b}),  # noqa: E501
        "client_feedback": ({"owner_id": s(a), "demand_id": demand, "decision": "interested"}, {a}),
        "screening_answered": ({"owner_id": s(b)}, {b}),
        "scorecard_due": ({"interviewer_user_id": s(a), "interview_id": s(uuid.uuid4())}, {a}),
        "interview_scheduled": ({"interviewer_user_ids": [s(a), s(b), s(admin)], "interview_id": s(uuid.uuid4())}, {a, b}),  # noqa: E501
        "offer_response": ({"created_by": s(a), "demand_id": demand, "outcome": "accepted"}, {a}),
        "import_completed": ({"created_by": s(b), "batch_id": s(uuid.uuid4())}, {b}),
        "rediscovery_ready": ({"demand_id": demand, "assignee_ids": [s(a), s(admin)]}, {a}),
        "task_assigned": ({"owner_id": s(b), "task_id": s(uuid.uuid4())}, {b}),
        "task_reminder": ({"owner_id": s(a), "task_id": s(uuid.uuid4())}, {a}),
        "email_failed": ({"sender_user_id": s(a), "message_id": s(uuid.uuid4())}, {a}),
        "email_bounced": ({"sender_user_id": s(b), "message_id": s(uuid.uuid4()), "bounce_type": "hard"}, {b}),  # noqa: E501
        "workflow_failed": ({"execution_id": s(uuid.uuid4()), "rule_id": s(uuid.uuid4()), "status": "failed"}, {a, b}),  # noqa: E501
        "export_ready": ({"requested_by": s(a), "export_id": s(uuid.uuid4())}, {a}),
        "workflow_notice": ({"recipient_ids": [s(a), s(b)], "message": "Heads up"}, {a, b}),
    }  # fmt: skip
    payload, expected = table[type_key]
    return {**payload, "actor_user_id": s(admin), "event_id": s(uuid.uuid4())}, expected


def test_every_registry_row_has_a_case() -> None:
    # Phase 1 types; Phase 2 epics register more from their hooks (Epic 13 registry).
    assert set(notifications_service.NOTIFICATION_TYPES) >= {
        "demand_assigned", "approval_requested", "approval_decided", "mention", "sla_at_risk", "sla_breached",
        "client_feedback", "screening_answered", "scorecard_due", "interview_scheduled", "offer_response",
        "import_completed", "rediscovery_ready", "task_assigned", "task_reminder", "email_failed",
        "email_bounced", "workflow_failed", "export_ready", "workflow_notice",
    }  # fmt: skip


# The Phase 1 registry rows covered by ``_payload``. Phase 2 hooks register more types (tested in their
# epics); the parametrisation must not depend on which hooks modules were imported at collection time.
P1_TYPES = (
    "approval_decided", "approval_requested", "client_feedback", "demand_assigned", "email_bounced",
    "email_failed", "export_ready", "import_completed", "interview_scheduled", "mention", "offer_response",
    "rediscovery_ready", "scorecard_due", "screening_answered", "sla_at_risk", "sla_breached",
    "task_assigned", "task_reminder", "workflow_failed", "workflow_notice",
)  # fmt: skip


@pytest.mark.parametrize("type_key", P1_TYPES)
async def test_registry_row_fan_out(norg: NOrg, type_key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import approvals

    async def fake_request(ctx: Any, db: Any, request_id: Any) -> Any:
        return SimpleNamespace(submitted_by=norg.a.user_id)

    monkeypatch.setattr(approvals, "get_request", fake_request)
    definition = notifications_service.NOTIFICATION_TYPES[type_key]
    payload, expected = _payload(norg, type_key)
    # B turns this type off (mandatory types: email only -- in-app stays on); A keeps the registry default.
    b_channel = "email" if definition.mandatory_in_app else "off"
    async with tenant_session(norg.tenant_id) as db:
        await db.execute(
            NotificationPreference.__table__.delete().where(NotificationPreference.user_id == norg.b.user_id)
        )
        db.add(
            NotificationPreference(
                id=uuid.uuid4(),
                tenant_id=norg.tenant_id,
                user_id=norg.b.user_id,
                type=type_key,
                channel=b_channel,
            )
        )
    async with tenant_session(norg.tenant_id) as db:
        await notifications_service.fan_out(db, type_key, payload)
    got = await rows(
        norg.tenant_id,
        "SELECT user_id, in_app FROM notifications WHERE type = :t AND dedupe_key LIKE :k",
        t=type_key,
        k=f"{type_key}:{payload['event_id']}:%",
    )
    notified = {r["user_id"]: r["in_app"] for r in got}
    expected_rows = set(expected)
    if norg.b.user_id in expected and not definition.mandatory_in_app:
        expected_rows.discard(norg.b.user_id)  # B turned it off
    assert set(notified) == expected_rows, type_key
    assert norg.org.admin.user_id not in notified  # the actor is never notified
    assert all(notified.values())  # in-app rows (A default, B mandatory)
    emails = await rows(
        norg.tenant_id,
        "SELECT r.address FROM email_recipients r JOIN notification_deliveries d ON d.email_message_id = r.message_id "  # noqa: E501
        "JOIN notifications n ON n.id = d.notification_id WHERE n.type = :t AND n.dedupe_key LIKE :k",
        t=type_key,
        k=f"{type_key}:{payload['event_id']}:%",
    )
    addresses = {str(e["address"]) for e in emails}
    a_email = norg.a.user_id in expected and definition.default_channel in ("email", "both")
    b_email = norg.b.user_id in expected and definition.mandatory_in_app
    assert (norg.a.email.lower() in addresses) == a_email, type_key
    assert (norg.b.email.lower() in addresses) == b_email, type_key


async def test_same_event_twice_notifies_once(norg: NOrg) -> None:
    from app.core.events import _dispatch

    before = await count(
        norg.tenant_id, "notifications", "type = 'mention' AND user_id = :u", u=norg.a.user_id
    )
    event = UserMentioned(
        tenant_id=norg.tenant_id,
        activity_id=uuid.uuid4(),
        entity_type="demand",
        entity_id=norg.demand_id,
        mentioned_user_id=norg.a.user_id,
        author_id=norg.org.admin.user_id,
    )
    _dispatch(event)  # after-commit dispatch -> eager fan-out
    _dispatch(event)  # the same event delivered again
    after = await count(
        norg.tenant_id, "notifications", "type = 'mention' AND user_id = :u", u=norg.a.user_id
    )
    assert after == before + 1


async def test_mention_through_a_note(norg: NOrg) -> None:
    before = await count(
        norg.tenant_id, "notifications", "type = 'mention' AND user_id = :u", u=norg.b.user_id
    )
    resp = await norg.org.admin.client.post(
        f"{API}/demands/{norg.demand_id}/activities",
        json={"body": f"Please review @[Ben Beta](user:{norg.b.user_id})"},
        headers=h(norg.org.admin),
    )
    assert resp.status_code == 201, resp.text
    assert (
        await count(norg.tenant_id, "notifications", "type = 'mention' AND user_id = :u", u=norg.b.user_id)
        == before + 1
    )
    listed = await norg.b.client.get(NOTIFICATIONS, params={"unread_only": True})
    assert any(
        n["type"] == "mention" and n["link_path"] == f"/demands/{norg.demand_id}"
        for n in listed.json()["items"]
    )


async def test_preferences(norg: NOrg, app: FastAPI) -> None:
    m = await join(app, norg.org.tenant, "recruiter", name="Pat Prefs")
    resp = await m.client.get(PREFS)
    assert resp.status_code == 200
    items = {i["type"]: i for i in resp.json()["items"]}
    assert items["mention"]["mandatory"] is True and items["mention"]["channel"] == "both"
    resp = await m.client.put(PREFS, json={"items": [{"type": "mention", "channel": "off"}]}, headers=h(m))
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "notification_mandatory"
    resp = await m.client.put(
        PREFS,
        json={"items": [{"type": "mention", "channel": "in_app"}, {"type": "task_assigned", "channel": "off"}],  # noqa: E501
              "digest": {"enabled": True, "hour_local": 7}},
        headers=h(m),
    )  # fmt: skip
    assert resp.status_code == 200, resp.text
    items = {i["type"]: i for i in resp.json()["items"]}
    assert items["mention"]["channel"] == "in_app" and items["task_assigned"]["channel"] == "off"
    assert resp.json()["digest"] == {"enabled": True, "hour_local": 7, "timezone": "Asia/Kolkata"}
    bad = await m.client.put(PREFS, json={"items": [{"type": "nope", "channel": "off"}]}, headers=h(m))
    assert bad.status_code == 422
    await m.client.aclose()


async def test_reads_count_cap_and_access(norg: NOrg, other_dorg: DOrg) -> None:
    m = norg.b
    now = datetime.now(UTC)
    async with tenant_session(norg.tenant_id) as db:
        await db.execute(update(Notification).where(Notification.user_id == m.user_id).values(read_at=now))
        for i in range(150):
            db.add(
                Notification(
                    id=uuid.uuid4(), tenant_id=norg.tenant_id, user_id=m.user_id, type="workflow_notice",
                    title=f"n{i}", dedupe_key=f"bulk:{uuid.uuid4()}", created_at=now - timedelta(seconds=i),
                )
            )  # fmt: skip
    resp = await m.client.get(f"{NOTIFICATIONS}/unread-count")
    assert resp.status_code == 200 and resp.json()["count"] == 100
    assert resp.headers["cache-control"] == "no-store" and resp.json()["latest_created_at"] is not None
    page = (await m.client.get(NOTIFICATIONS, params={"unread_only": True, "page_size": 5})).json()
    assert page["total"] == 150 and len(page["items"]) == 5
    first = page["items"][0]["id"]
    assert (await m.client.post(f"{NOTIFICATIONS}/{first}/read", headers=h(m))).json()["read_at"] is not None
    # somebody else's notification is invisible; so is another tenant's
    assert (await norg.a.client.post(f"{NOTIFICATIONS}/{first}/read", headers=h(norg.a))).status_code == 404
    assert (
        await other_dorg.admin.client.post(f"{NOTIFICATIONS}/{first}/read", headers=h(other_dorg.admin))
    ).status_code == 404
    assert (
        await other_dorg.admin.client.delete(f"{NOTIFICATIONS}/{first}", headers=h(other_dorg.admin))
    ).status_code == 404
    assert (await m.client.delete(f"{NOTIFICATIONS}/{first}", headers=h(m))).status_code == 204
    assert (await m.client.post(f"{NOTIFICATIONS}/read-all", headers=h(m))).json()["count"] == 0
    assert (await m.client.get(f"{NOTIFICATIONS}/unread-count")).json()["count"] == 0


async def _digest_settings(n: NOrg, user: Member, hour: int, last: date | None = None) -> None:
    async with tenant_session(n.tenant_id) as db:
        row = await db.get(NotificationSettings, (n.tenant_id, user.user_id))
        if row is None:
            db.add(
                NotificationSettings(
                    tenant_id=n.tenant_id,
                    user_id=user.user_id,
                    digest_enabled=True,
                    digest_hour_local=hour,
                    last_digest_date=last,
                )
            )
        else:
            row.digest_enabled, row.digest_hour_local, row.last_digest_date = True, hour, last


async def _digest_emails(n: NOrg, user: Member) -> int:
    return await count(
        n.tenant_id,
        "email_messages m",
        "m.origin = 'system' AND m.subject LIKE '%daily summary%' AND EXISTS "
        "(SELECT 1 FROM email_recipients r WHERE r.message_id = m.id AND r.address = :a)",
        a=user.email.lower(),
    )


async def test_digest(norg: NOrg, app: FastAPI) -> None:
    from zoneinfo import ZoneInfo

    user = await join(app, norg.org.tenant, "recruiter", name="Dee Digest")
    tz = ZoneInfo("Asia/Kolkata")
    now = datetime.now(UTC)
    hour = now.astimezone(tz).hour
    # nothing to report -> no email, but the day is marked
    await _digest_settings(norg, user, hour)
    async with tenant_session(norg.tenant_id) as db:
        await notifications_service.run_digest(db, now=now)
    assert await _digest_emails(norg, user) == 0
    # an overdue task -> one email at the local digest hour, once per day
    resp = await user.client.post(
        TASKS,
        json={"title": "Overdue call", "due_at": (now - timedelta(days=1)).isoformat()},
        headers=h(user),
    )
    assert resp.status_code == 201, resp.text
    await _digest_settings(norg, user, (hour + 3) % 24)  # wrong hour
    async with tenant_session(norg.tenant_id) as db:
        assert await notifications_service.run_digest(db, now=now) == 0
    await _digest_settings(norg, user, hour, last=None)
    async with tenant_session(norg.tenant_id) as db:
        assert await notifications_service.run_digest(db, now=now) >= 1
    async with tenant_session(norg.tenant_id) as db:
        await notifications_service.run_digest(db, now=now)  # same day: skipped
    assert await _digest_emails(norg, user) == 1
    (msg,) = await rows(
        norg.tenant_id,
        "SELECT m.body_html FROM email_messages m JOIN email_recipients r ON r.message_id = m.id "
        "WHERE r.address = :a AND m.subject LIKE '%daily summary%'",
        a=user.email.lower(),
    )
    assert "Overdue call" in msg["body_html"] and "Overdue tasks" in msg["body_html"]
    await user.client.aclose()


async def test_purge_old(norg: NOrg) -> None:
    now = datetime.now(UTC)
    async with tenant_session(norg.tenant_id) as db:
        for age, read in ((100, True), (100, False), (200, False), (10, True)):
            db.add(
                Notification(
                    id=uuid.uuid4(), tenant_id=norg.tenant_id, user_id=norg.a.user_id, type="workflow_notice",
                    title=f"purge-{age}-{read}", dedupe_key=f"purge:{uuid.uuid4()}",
                    created_at=now - timedelta(days=age), read_at=now if read else None,
                )
            )  # fmt: skip
    async with tenant_session(norg.tenant_id) as db:
        await notifications_service.purge_old(db, now=now)
    left = {
        r["title"]
        for r in await rows(norg.tenant_id, "SELECT title FROM notifications WHERE title LIKE 'purge-%'")
    }
    assert left == {"purge-100-False", "purge-10-True"}


async def test_cross_tenant_events_never_notify_other_tenant(norg: NOrg, other_dorg: DOrg) -> None:
    payload, _ = _payload(norg, "task_assigned")
    payload["owner_id"] = str(other_dorg.admin.user_id)  # a user of another tenant
    async with tenant_session(norg.tenant_id) as db:
        assert await notifications_service.fan_out(db, "task_assigned", payload) == 0
    assert await count(other_dorg.tenant_id, "notifications") == 0
