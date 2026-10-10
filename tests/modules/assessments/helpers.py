# ruff: noqa: E501
"""Shared helpers of the Epic 40 assessment tests."""

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import CATALOG, Principal, Scope
from tests.epic12 import sent
from tests.modules.demands.helpers import DOrg, h

API = "/api/v1"
ITEMS = f"{API}/assessment-items"
ASM = f"{API}/assessments"
INV = f"{API}/assessment-invitations"
PUBLIC = f"{API}/public/links"
TOKEN_RE = re.compile(r"/l/([A-Za-z0-9_\-]+)")
SESSION = "X-Attempt-Session"


def item_body(kind: str, **kw: Any) -> dict[str, Any]:
    """A valid item of each kind (answer keys documented in items.py)."""
    bodies: dict[str, dict[str, Any]] = {
        "single": {"stem": "2 + 2 = ?", "options": {"choices": [{"id": "a", "text": "3"}, {"id": "b", "text": "4"}, {"id": "c", "text": "5"}]}, "answer_key": {"correct": "b"}},
        "multi": {"stem": "Primes?", "options": {"choices": [{"id": "a", "text": "2"}, {"id": "b", "text": "4"}, {"id": "c", "text": "5"}]}, "answer_key": {"correct": ["a", "c"]}},
        "true_false": {"stem": "Water is wet", "options": {"choices": [{"id": "t", "text": "True"}, {"id": "f", "text": "False"}]}, "answer_key": {"correct": "t"}},
        "numeric": {"stem": "pi to 2dp", "answer_key": {"value": "3.14", "tolerance_abs": "0.01"}},
        "short_text": {"stem": "Capital of France?", "answer_key": {"accepted": ["Paris"], "keywords": [{"term": "paris", "weight": "1"}]}},
        "code_output": {"stem": "What prints?", "language": "python", "code": "print(1 + 1)", "answer_key": {"accepted": ["2"]}},
        "code_fill": {"stem": "Fill", "language": "python", "code": "for i in {{blank:1}}(3):\n    print(i)", "answer_key": {"blanks": {"1": {"accepted": ["range"]}}}},
        "code_order": {"stem": "Order", "options": {"lines": [{"id": "l1", "text": "def f():"}, {"id": "l2", "text": "    x = 1"}, {"id": "l3", "text": "    return x"}]}, "answer_key": {"order": ["l1", "l2", "l3"], "partial": "lcs"}},
        "code_find_bug": {"stem": "Find the bug", "language": "python", "code": "a = 1\nb = 0\nprint(a / b)", "answer_key": {"bug_lines": [2]}},
        "code_freeform": {"stem": "Write fizzbuzz", "language": "python", "rubric": {"criteria": [{"key": "c1", "label": "Correct", "max_points": "3"}, {"key": "c2", "label": "Clean", "max_points": "2"}]}},
        "essay": {"stem": "Explain REST", "rubric": {"criteria": [{"key": "c1", "label": "Clarity", "max_points": "5"}]}},
    }
    return {"kind": kind, "points": "5" if kind in ("code_freeform", "essay") else "1", "difficulty": 3, **bodies[kind], **kw}


async def create_item(org: DOrg, kind: str, **kw: Any) -> dict[str, Any]:
    r = await org.admin.client.post(ITEMS, json=item_body(kind, **kw), headers=h(org.admin))
    assert r.status_code == 201, r.text
    return dict(r.json())


async def create_assessment(org: DOrg, name: str = "Python screen", **kw: Any) -> dict[str, Any]:
    body = {"name": name, "delivery": "builtin", "time_limit_minutes": 30, "gating_default": True, **kw}
    r = await org.admin.client.post(ASM, json=body, headers=h(org.admin))
    assert r.status_code == 201, r.text
    return dict(r.json())


async def published(org: DOrg, sections: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    a = await create_assessment(org, **kw)
    r = await org.admin.client.put(f"{ASM}/{a['id']}/draft", json={"sections": sections}, headers=h(org.admin))
    assert r.status_code == 200, r.text
    p = await org.admin.client.post(f"{ASM}/{a['id']}/publish", headers=h(org.admin))
    assert p.status_code == 200, p.text
    return {**a, "version": p.json()["version"], "warnings": p.json()["warnings"]}


async def simple_assessment(org: DOrg, kinds: tuple[str, ...] = ("single", "short_text", "code_output"), **kw: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    items = [await create_item(org, k) for k in kinds]
    a = await published(org, [{"key": "s1", "title": "Main", "items": [{"item_id": i["id"]} for i in items]}], **kw)
    return a, items


async def make_candidate(org: DOrg, name: str = "Asha Candidate", email: str | None = None, **body: Any) -> uuid.UUID:
    email = email or f"asha-{uuid.uuid4().hex[:8]}@example.com"
    r = await org.admin.client.post(f"{API}/candidates", json={"full_name": name, "emails": [{"email": email}], **body}, headers=h(org.admin))
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"])


async def invite(org: DOrg, assessment_id: str, candidate_id: uuid.UUID, **kw: Any) -> Any:
    body = {"assessment_id": assessment_id, "candidate_id": str(candidate_id), "context_kind": "direct", **kw}
    return await org.admin.client.post(INV, json=body, headers=h(org.admin))


async def invite_ok(org: DOrg, assessment_id: str, candidate_id: uuid.UUID, **kw: Any) -> dict[str, Any]:
    r = await invite(org, assessment_id, candidate_id, **kw)
    assert r.status_code == 201, r.text
    return dict(r.json())


async def candidate_email(org: DOrg, candidate_id: uuid.UUID) -> str:
    async with tenant_session(org.tenant_id) as db:
        return str((await db.execute(text("SELECT email FROM candidate_emails WHERE candidate_id = :c"), {"c": candidate_id})).scalar_one())


async def link_token(org: DOrg, candidate_id: uuid.UUID) -> str:
    email = await candidate_email(org, candidate_id)
    msgs = sent(kind="assessment_invited", to=email)
    assert msgs, f"no assessment invitation sent to {email}"
    m = TOKEN_RE.search(msgs[-1].text)
    assert m is not None
    return m.group(1)


async def start(client: Any, token: str) -> dict[str, Any]:
    pre = await client.get(f"{PUBLIC}/{token}/assessment")
    assert pre.status_code == 200, pre.text
    r = await client.post(f"{PUBLIC}/{token}/assessment/start", json={"notice_id": pre.json()["notice"]["notice_id"], "agreed": True})
    assert r.status_code == 200, r.text
    return dict(r.json())


def answer_for(item: dict[str, Any], kind: str, *, correct: bool = True) -> dict[str, Any]:
    """A response to a delivered item (opaque ids are mapped from the delivered texts)."""
    if kind in ("single", "true_false"):
        target = {"single": "4", "true_false": "True"}[kind] if correct else None
        opts = item["options"]
        choice = next((o["id"] for o in opts if o["text"] == target), opts[0]["id"] if not correct else None)
        if not correct:
            choice = next(o["id"] for o in opts if o["text"] not in ("4", "True"))
        return {"choice": choice}
    if kind == "short_text":
        return {"text": "Paris" if correct else "London"}
    if kind == "code_output":
        return {"text": "2" if correct else "11"}
    if kind == "code_fill":
        return {"blanks": {"1": "range" if correct else "list"}}
    if kind == "code_order":
        by_text = {line["text"]: line["id"] for line in item["lines"]}
        order = [by_text["def f():"], by_text["    x = 1"], by_text["    return x"]]
        return {"order": order if correct else list(reversed(order))}
    if kind == "code_find_bug":
        return {"lines": [2] if correct else [1]}
    if kind == "numeric":
        return {"value": "3.14" if correct else "3"}
    if kind == "multi":
        ids = [o["id"] for o in item["options"] if o["text"] in ("2", "5")]
        return {"choices": ids if correct else ids[:1]}
    return {"text": "def fizzbuzz(): pass"}


async def save(client: Any, token: str, session: str, seq: int, answers: list[dict[str, Any]]) -> Any:
    return await client.put(f"{PUBLIC}/{token}/assessment/attempt/answers", json={"client_seq": seq, "answers": answers}, headers={SESSION: session})


async def submit(client: Any, token: str, session: str) -> Any:
    return await client.post(f"{PUBLIC}/{token}/assessment/attempt/submit", headers={SESSION: session})


async def shift_attempt(org: DOrg, invitation_id: str, minutes: int) -> None:
    """Move the attempt's clock: deadline / start ``minutes`` into the past (server-side timer tests)."""
    async with tenant_session(org.tenant_id) as db:
        await db.execute(
            text("UPDATE attempts SET deadline_at = deadline_at - make_interval(mins => :m), started_at = started_at - make_interval(mins => :m) WHERE invitation_id = :i"),
            {"m": minutes, "i": uuid.UUID(invitation_id)},
        )


async def sql(org: DOrg, stmt: str, **params: Any) -> list[Any]:
    async with tenant_session(org.tenant_id) as db:
        res = await db.execute(text(stmt), params)
        return list(res.all()) if res.returns_rows else []


def ctx(org: DOrg, *, actor_kind: str | None = None, principal_type: str = "user") -> RequestContext:
    principal = Principal(type=principal_type, id=org.admin.user_id, tenant_id=org.tenant_id, permissions=dict.fromkeys(CATALOG, Scope.TENANT))  # type: ignore[arg-type]
    return RequestContext(tenant_id=org.tenant_id, principal=principal, actor_kind=actor_kind, actor_id=uuid.uuid4() if actor_kind else None)


def soon(days: int = 3) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


__all__ = [
    "API", "ASM", "INV", "ITEMS", "PUBLIC", "SESSION", "answer_for", "candidate_email", "create_assessment", "create_item",
    "ctx", "invite", "invite_ok", "item_body", "link_token", "make_candidate", "published", "save", "shift_attempt",
    "simple_assessment", "soon", "sql", "start", "submit",
]  # fmt: skip
