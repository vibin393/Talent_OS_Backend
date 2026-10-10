# ruff: noqa: E501
"""Epic 52 (A6, FR-263): Phase 3 go-live import kinds through the existing go-live flow -- templates and dictionaries,
preview row errors mapped to columns, apply idempotent per row (``result_ref``), ``depends_on`` order (422
``dependency_missing``, nothing applied), masked sensitive columns, unavailable stub owners."""

import csv
import io
import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from app.core.database import tenant_session
from app.models.campus import Institution
from app.models.clients import Client
from app.models.projects import Project, ProjectRole
from app.services.golive import p3_imports
from tests.modules.demands.helpers import DOrg, h
from tests.modules.immigration.helpers import adopt
from tests.modules.offers.helpers import create_candidate

GL = "/api/v1/golive"
P3_KINDS = [
    "institutions", "assessment_items", "workforce_plan_lines", "projects", "project_roles", "project_assignments",
    "immigration_cases", "referral_history",
]  # fmt: skip


def _csv(headers: list[str], rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode()


async def _upload(org: DOrg, kind: str, data: bytes) -> Any:
    return await org.admin.client.post(
        f"{GL}/imports",
        data={"kind": kind},
        files={"file": ("data.csv", data, "text/csv")},
        headers=h(org.admin),
    )


async def _apply(org: DOrg, batch_id: str, *, skip_invalid: bool = False) -> Any:
    return await org.admin.client.post(
        f"{GL}/imports/{batch_id}/apply",
        json={"skip_invalid": skip_invalid, "confirm": "APPLY"},
        headers={**h(org.admin), "Idempotency-Key": uuid.uuid4().hex},
    )


async def _client_name(org: DOrg) -> str:
    async with tenant_session(org.tenant_id) as db:
        return str((await db.execute(select(Client.name).where(Client.id == org.client_id))).scalar_one())


async def test_p3_kinds_templates_and_dictionaries(dorg: DOrg) -> None:
    c = dorg.admin.client
    kinds = {k["kind"]: k for k in (await c.get(f"{GL}/kinds")).json()}
    assert set(P3_KINDS) <= set(kinds)
    assert kinds["project_roles"]["depends_on"] == ["projects"]
    assert kinds["project_assignments"]["depends_on"] == ["project_roles", "placements"]
    assert kinds["immigration_cases"]["depends_on"] == ["workers"]
    for kind in P3_KINDS:
        if kind != "referral_history":
            assert kinds[kind]["available"] is True, kind
        resp = await c.get(f"{GL}/templates/{kind}")
        assert resp.status_code == 200
        header, example = list(csv.reader(io.StringIO(resp.text)))
        assert header == [col.name for col in p3_imports.P3_KINDS[kind].columns]
        assert len(example) == len(header)
        dictionary = (await c.get(f"{GL}/templates/{kind}/dictionary")).json()
        assert [d["column"] for d in dictionary] == header


async def test_owner_stub_or_missing_hides_kind(dorg: DOrg) -> None:
    """``referrals_service.import_referral`` does not exist yet (Epic 42): the kind is listed, unavailable, refused."""
    kinds = {k["kind"]: k for k in (await dorg.admin.client.get(f"{GL}/kinds")).json()}
    assert kinds["referral_history"]["available"] is False
    resp = await _upload(dorg, "referral_history", _csv(["referrer_email"], [["a@b.co"]]))
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "kind_unavailable"


async def test_institutions_preview_errors_and_apply(dorg: DOrg) -> None:
    name = f"Inst {uuid.uuid4().hex[:8]}"
    data = _csv(
        ["name", "city", "country", "tier", "contact_name", "contact_email"],
        [
            [name, "Chennai", "IN", "tier_1", "R Kumar", "tpo@example.com"],
            ["X", "Pune", "IN", "tier_9", "", "not-an-email"],
        ],
    )
    batch = (await _upload(dorg, "institutions", data)).json()
    assert batch["row_count"] == 2 and batch["error_count"] == 1, batch["errors"]
    cols = {e["column"] for e in batch["errors"] if e["row"] == 2}
    assert cols & {"name", "tier", "contact_email"}
    done = await _apply(dorg, batch["id"], skip_invalid=True)
    assert done.status_code == 200, done.text
    assert done.json()["applied_count"] == 1
    async with tenant_session(dorg.tenant_id) as db:
        assert (await db.execute(select(Institution.id).where(Institution.name == name))).scalar_one()
    again = await _apply(dorg, batch["id"], skip_invalid=True)
    assert again.status_code == 409  # applied batches are never applied twice (row result_ref kept)
    rows = (await dorg.admin.client.get(f"{GL}/imports/{batch['id']}/rows")).json()
    items = rows["items"] if isinstance(rows, dict) else rows
    applied = [r for r in items if r["status"] == "applied"]
    assert applied and str(applied[0].get("result_ref", "")).startswith("institutions:")


async def test_projects_roles_assignments_dependency_order(dorg: DOrg) -> None:
    client_name = await _client_name(dorg)
    pname = f"Proj {uuid.uuid4().hex[:8]}"
    # Assignments before roles: preview pending (no row error), apply -> 422 dependency_missing, nothing applied.
    start = (date.today() - timedelta(days=10)).isoformat()
    projects = (await _upload(dorg, "projects", _csv(["name", "client_name"], [[pname, client_name]]))).json()
    assert projects["error_count"] == 0, projects
    assert (await _apply(dorg, projects["id"])).status_code == 200
    async with tenant_session(dorg.tenant_id) as db:
        human_id = (await db.execute(select(Project.human_id).where(Project.name == pname))).scalar_one()

    assignments = (
        await _upload(
            dorg,
            "project_assignments",
            _csv(
                ["project_human_id", "role_title", "placement_human_id", "start_date"],
                [[human_id, "Data Engineer", "PLC-999999", start]],
            ),
        )
    ).json()
    assert assignments["error_count"] == 0, assignments
    blocked = await _apply(dorg, assignments["id"])
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["error"]["code"] == "dependency_missing"
    assert "project_roles" in blocked.json()["error"]["details"]["requires"]

    roles_before = (
        await _upload(dorg, "project_roles", _csv(["project_human_id", "title"], [["PRJ-NOPE99", "Dev"]]))
    ).json()
    assert roles_before["error_count"] == 0
    refused = await _apply(dorg, roles_before["id"])
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "dependency_missing"

    roles = (
        await _upload(
            dorg,
            "project_roles",
            _csv(["project_human_id", "title", "headcount"], [[human_id, "Data Engineer", "2"]]),
        )
    ).json()
    assert roles["error_count"] == 0, roles
    done = await _apply(dorg, roles["id"])
    assert done.status_code == 200 and done.json()["applied_count"] == 1, done.text
    async with tenant_session(dorg.tenant_id) as db:
        role = (
            (await db.execute(select(ProjectRole).where(ProjectRole.title == "Data Engineer")))
            .scalars()
            .all()
        )
        assert any(r.headcount == 2 for r in role)


async def test_immigration_permit_number_masked_in_error_csv(dorg: DOrg) -> None:
    vt = await adopt(dorg)
    cand = await create_candidate(dorg.admin, "Imran Import")
    secret = "AB1234567890123456789012345678XY"  # > 30 characters: invalid format
    data = _csv(
        ["candidate_id", "visa_type_key", "permit_type", "permit_number", "permit_valid_from"],
        [[str(cand), vt["key"], "Employment", secret, date.today().isoformat()]],
    )
    batch = (await _upload(dorg, "immigration_cases", data)).json()
    assert batch["error_count"] == 1, batch
    assert batch["errors"][0]["column"] == "permit_number"
    err = await dorg.admin.client.get(f"{GL}/imports/{batch['id']}/errors.csv")
    assert err.status_code == 200
    assert secret not in err.text and secret[-4:] in err.text


async def test_immigration_needs_workers_first(dorg: DOrg) -> None:
    vt = await adopt(dorg)
    email = f"nobody-{uuid.uuid4().hex[:8]}@example.com"
    batch = (
        await _upload(
            dorg, "immigration_cases", _csv(["worker_email", "visa_type_key"], [[email, vt["key"]]])
        )
    ).json()
    assert batch["error_count"] == 0, batch
    blocked = await _apply(dorg, batch["id"])
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "dependency_missing"
