"""Epic 2 (story 2.3): notes, mentions, edit window, private notes, timeline pagination, system entries."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.events import subscribe, unsubscribe
from app.events.activity import UserMentioned
from app.models.activity import Activity
from app.services import activity_service
from tests.epic2 import JoinAs
from tests.modules.identity.conftest import Member, Org

CLIENTS = "/api/v1/clients"
ACTIVITIES = "/api/v1/activities"


async def _new_client(m: Member, name: str = "Timeline Client") -> str:
    resp = await m.client.post(CLIENTS, json={"name": name}, headers=m.h())
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _note(m: Member, client_id: str, body: str, **extra: Any) -> dict[str, Any]:
    resp = await m.client.post(
        f"{CLIENTS}/{client_id}/activities", json={"body": body, **extra}, headers=m.h()
    )
    assert resp.status_code == 201, resp.text
    out: dict[str, Any] = resp.json()
    return out


@pytest.fixture
def mentions() -> Any:
    seen: list[UserMentioned] = []
    subscribe(UserMentioned, seen.append)
    yield seen
    unsubscribe(UserMentioned, seen.append)


async def test_note_with_mention_emits_event(
    org: Org, join_as: JoinAs, mentions: list[UserMentioned]
) -> None:
    colleague = await join_as("recruiter", name="Cole League")
    client_id = await _new_client(org.admin)
    body = (
        f"Please follow up @[Cole League](user:{colleague.user_id}) "
        f"and @[Ada Admin](user:{org.admin.user_id})"
    )
    note = await _note(org.admin, client_id, body)
    assert note["mentions"] == [str(colleague.user_id), str(org.admin.user_id)]
    assert (note["kind"], note["author_name"], note["can_edit"]) == ("note", "Ada Admin", True)
    # Self-mentions are not notified.
    assert [(e.mentioned_user_id, e.entity_type) for e in mentions] == [(colleague.user_id, "client")]


async def test_mention_of_unknown_or_foreign_user_is_422(org: Org, other: Org) -> None:
    client_id = await _new_client(org.admin)
    for user_id in (uuid.uuid4(), other.admin.user_id):
        resp = await org.admin.client.post(
            f"{CLIENTS}/{client_id}/activities",
            json={"body": f"hey @[Someone](user:{user_id})"},
            headers=org.admin.h(),
        )
        assert (resp.status_code, resp.json()["error"]["code"]) == (422, "invalid_mention")


async def test_calls_and_meetings_can_be_logged_with_participants(org: Org) -> None:
    client_id = await _new_client(org.admin)
    when = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    call = await _note(
        org.admin,
        client_id,
        "Discussed Q4 hiring plan",
        kind="call",
        occurred_at=when,
        participants=[{"kind": "other", "name": "Hana"}],
    )
    assert call["kind"] == "call"
    assert call["participants"] == [{"kind": "other", "id": None, "name": "Hana"}]
    bad = await org.admin.client.post(
        f"{CLIENTS}/{client_id}/activities", json={"body": "x", "kind": "system"}, headers=org.admin.h()
    )
    assert bad.status_code == 422


async def test_edit_window_and_author_only(org: Org, join_as: JoinAs) -> None:
    client_id = await _new_client(org.admin)
    note = await _note(org.admin, client_id, "First draft")
    edited = await org.admin.client.patch(
        f"{ACTIVITIES}/{note['id']}", json={"body": "Second draft"}, headers=org.admin.h()
    )
    assert edited.status_code == 200, edited.text
    assert (edited.json()["body"], edited.json()["edited_at"] is not None) == ("Second draft", True)

    recruiter = await join_as("recruiter", name="Rae Cruiter")
    other_edit = await recruiter.client.patch(
        f"{ACTIVITIES}/{note['id']}", json={"body": "hijack"}, headers=recruiter.h()
    )
    assert (other_edit.status_code, other_edit.json()["error"]["code"]) == (403, "not_author")

    async with tenant_session(org.tenant.id) as db:
        await db.execute(
            update(Activity)
            .where(Activity.id == uuid.UUID(note["id"]))
            .values(created_at=datetime.now(UTC) - timedelta(hours=25))
        )
    late = await org.admin.client.patch(
        f"{ACTIVITIES}/{note['id']}", json={"body": "Too late"}, headers=org.admin.h()
    )
    assert (late.status_code, late.json()["error"]["code"]) == (403, "edit_window_closed")
    timeline = (await org.admin.client.get(f"{CLIENTS}/{client_id}/timeline")).json()
    assert next(i for i in timeline["items"] if i["id"] == note["id"])["can_edit"] is False


async def test_system_entries_cannot_be_edited(org: Org) -> None:
    client_id = await _new_client(org.admin)
    timeline = (await org.admin.client.get(f"{CLIENTS}/{client_id}/timeline")).json()
    (system,) = timeline["items"]
    assert (system["kind"], system["body"]) == ("system", "Client created")
    resp = await org.admin.client.patch(
        f"{ACTIVITIES}/{system['id']}", json={"body": "x"}, headers=org.admin.h()
    )
    assert resp.status_code == 403


async def test_private_notes_visible_to_author_and_admin_only(org: Org, join_as: JoinAs) -> None:
    client_id = await _new_client(org.admin)
    author = await join_as("account_manager", name="Pria Vate")
    peer = await join_as("recruiter", name="Peer Recruiter")
    secret = await _note(author, client_id, "Private thoughts", is_private=True)
    await _note(author, client_id, "Public note")

    def ids(resp_json: dict[str, Any]) -> set[str]:
        return {i["id"] for i in resp_json["items"]}

    assert secret["id"] in ids((await author.client.get(f"{CLIENTS}/{client_id}/timeline")).json())
    assert secret["id"] not in ids((await peer.client.get(f"{CLIENTS}/{client_id}/timeline")).json())
    assert secret["id"] in ids((await org.admin.client.get(f"{CLIENTS}/{client_id}/timeline")).json())
    hidden_edit = await peer.client.patch(
        f"{ACTIVITIES}/{secret['id']}", json={"body": "x"}, headers=peer.h()
    )
    assert hidden_edit.status_code == 404
    peer_360 = (await peer.client.get(f"{CLIENTS}/{client_id}/360")).json()
    admin_360 = (await org.admin.client.get(f"{CLIENTS}/{client_id}/360")).json()
    assert admin_360["counts"]["activities"] == peer_360["counts"]["activities"] + 1


async def test_timeline_cursor_pagination_newest_first(org: Org) -> None:
    client_id = await _new_client(org.admin)
    base = datetime.now(UTC) - timedelta(hours=1)
    for n in range(5):
        await _note(org.admin, client_id, f"note {n}", occurred_at=(base + timedelta(minutes=n)).isoformat())
    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = (await org.admin.client.get(f"{CLIENTS}/{client_id}/timeline", params=params)).json()
        seen += [i["body"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == ["Client created", "note 4", "note 3", "note 2", "note 1", "note 0"]
    bad = await org.admin.client.get(f"{CLIENTS}/{client_id}/timeline", params={"cursor": "!!nope"})
    assert (bad.status_code, bad.json()["error"]["code"]) == (422, "invalid_cursor")


async def test_viewer_can_read_but_not_write_notes(org: Org, join_as: JoinAs) -> None:
    client_id = await _new_client(org.admin)
    viewer = await join_as("viewer", name="Vi Ewer")
    assert (await viewer.client.get(f"{CLIENTS}/{client_id}/timeline")).status_code == 200
    resp = await viewer.client.post(
        f"{CLIENTS}/{client_id}/activities", json={"body": "hi"}, headers=viewer.h()
    )
    assert (resp.status_code, resp.json()["error"]["details"]["permission"]) == (403, "note:write")


async def test_unknown_entity_type_or_record_is_404(org: Org) -> None:
    c = org.admin.client
    assert (await c.get(f"/api/v1/widgets/{uuid.uuid4()}/timeline")).status_code == 404
    assert (await c.get(f"{CLIENTS}/{uuid.uuid4()}/timeline")).status_code == 404
    resp = await c.post(f"{CLIENTS}/{uuid.uuid4()}/activities", json={"body": "x"}, headers=org.admin.h())
    assert resp.status_code == 404


async def test_service_api_for_other_modules(org: Org) -> None:
    client_id = uuid.UUID(await _new_client(org.admin))
    target = uuid.UUID(await _new_client(org.admin, "Merge Target"))
    ctx = RequestContext()
    async with tenant_session(org.tenant.id) as db:
        entry = await activity_service.add_system_entry(
            ctx, db, "client", client_id, "Imported from CRM", {"source": "crm"}
        )
        moved = await activity_service.reassign_entity(ctx, db, "client", client_id, target)
        assert entry in moved and len(moved) == 2
        await activity_service.reassign_activities(ctx, db, "client", [entry], client_id)
    timeline = (await org.admin.client.get(f"{CLIENTS}/{client_id}/timeline")).json()["items"]
    assert [(i["body"], i["data"], i["author_id"]) for i in timeline] == [
        ("Imported from CRM", {"source": "crm"}, None)
    ]
    assert "clients" in activity_service.registered_entities()


def test_parse_mentions() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    body = f"@[A](user:{a}) x @[B B](user:{b}) again @[A](user:{a}) @[bad](user:123) @[C](team:{a})"
    assert activity_service.parse_mentions(body) == [a, b]
