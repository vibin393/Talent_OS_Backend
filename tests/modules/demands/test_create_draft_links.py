"""Epic 36 (spec 0.5): ``demands_service.create_draft`` links a draft Demand to a plan line / project role."""

import uuid
from datetime import date

import pytest

from app.core.database import tenant_session
from app.core.errors import ValidationFailed
from app.models.projects import Project, ProjectRole
from app.models.workforce import WorkforcePlan, WorkforcePlanLine
from app.schemas.demands import DemandCreate
from app.services import demands_service
from tests.modules.demands.helpers import DOrg, admin_ctx, create_client


async def _seed(org: DOrg, other_client: uuid.UUID) -> dict[str, uuid.UUID]:
    ids = {
        k: uuid.uuid4() for k in ("prj", "prj_other", "role", "role_other", "line", "line_any", "line_other")
    }
    t = org.tenant_id
    async with tenant_session(t) as db:
        db.add(Project(id=ids["prj"], tenant_id=t, human_id="PRJ-1", name="P", client_id=org.client_id))
        db.add(Project(id=ids["prj_other"], tenant_id=t, human_id="PRJ-2", name="Q", client_id=other_client))
        await db.flush()
        db.add(
            ProjectRole(
                id=ids["role"], tenant_id=t, project_id=ids["prj"], client_id=org.client_id, title="Dev"
            )
        )
        db.add(
            ProjectRole(
                id=ids["role_other"],
                tenant_id=t,
                project_id=ids["prj_other"],
                client_id=other_client,
                title="QA",
            )
        )
        await db.flush()
        for k, c in (("line", org.client_id), ("line_any", None), ("line_other", other_client)):
            # Epic 44 (308_01): lines belong to a real plan and inherit its client (tos_inherit_org).
            plan_id = uuid.uuid4()
            db.add(
                WorkforcePlan(
                    id=plan_id,
                    tenant_id=t,
                    name=f"Plan {k}",
                    period_start=date(2026, 10, 1),
                    period_end=date(2027, 3, 31),
                    client_id=c,
                    currency="INR",
                    owner_id=org.admin.user_id,
                )
            )
            await db.flush()
            db.add(
                WorkforcePlanLine(
                    id=ids[k],
                    tenant_id=t,
                    workforce_plan_id=plan_id,
                    client_id=c,
                    role_title="Dev",
                    period_start=date(2026, 11, 1),
                    period_end=date(2026, 12, 31),
                    headcount=1,
                )
            )
    return ids


async def test_create_draft_links_and_errors(dorg: DOrg) -> None:
    other = await create_client(dorg.admin, "Other Co")
    ids = await _seed(dorg, other)
    ctx = admin_ctx(dorg)
    data = DemandCreate(title="Linked Role", client_id=dorg.client_id)

    async with tenant_session(dorg.tenant_id) as db:
        out = await demands_service.create_draft(
            ctx, db, data, workforce_plan_line_id=ids["line"], project_role_id=ids["role"]
        )
    assert out["status"] == "draft" and out["visibility"] == "external"
    assert str(out["workforce_plan_line_id"]) == str(ids["line"]) and str(out["project_role_id"]) == str(
        ids["role"]
    )

    async with tenant_session(dorg.tenant_id) as db:
        out = await demands_service.create_draft(ctx, db, data, workforce_plan_line_id=ids["line_any"])
        plain = await demands_service.create_draft(ctx, db, data)
    assert str(out["workforce_plan_line_id"]) == str(ids["line_any"]) and out["project_role_id"] is None
    assert plain["workforce_plan_line_id"] is None

    cases = [
        ({"workforce_plan_line_id": uuid.uuid4()}, "unknown_plan_line"),
        ({"project_role_id": uuid.uuid4()}, "unknown_project_role"),
        ({"project_role_id": ids["role_other"]}, "project_role_client_mismatch"),
        ({"workforce_plan_line_id": ids["line_other"]}, "plan_line_client_mismatch"),
    ]
    for kwargs, code in cases:
        with pytest.raises(ValidationFailed) as exc:
            async with tenant_session(dorg.tenant_id) as db:
                await demands_service.create_draft(ctx, db, data, **kwargs)
        assert exc.value.code == code
