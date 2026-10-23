# ruff: file-ignore[line-too-long]
"""Phase 3 go-live import kinds (Epic 52 A6, FR-263): templates, owner resolution and row handlers.

Each kind delegates one CSV row to the owner's ``import_<x>(ctx, db, data: <X>ImportIn, *, validate_only)``
(foundation 0.7) through the existing ``file_import_batches`` flow (preview = validate-only, apply per row with
``result_ref``). Column names are the owner DTO's field names (owner names win over the spec table; deviations are
recorded in the Epic 52 notes). Registered additively into ``templates.KINDS``, ``owners.owner()`` resolution
(``owners.EXTRA_OWNERS``) and ``owners.HANDLERS`` when ``app.services.golive`` is imported.

Dependencies between kinds (``depends_on``): a row whose reference to a ``depends_on`` kind is missing raises
``owners.DependencyPending`` (preview: the row is valid but pending; apply: 422 ``dependency_missing`` before any
row is applied). A kind whose owner function is missing or still a stub is ``available=False`` (``referral_history``
until Epic 42 ships ``referrals_service.import_referral``).
"""

import uuid
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import DomainError
from app.services.golive import owners, templates
from app.services.golive.templates import Column, KindDef

C = Column

# kind -> (facade module, fallback module, function name, DTO "module:Class")
P3_OWNERS: dict[str, tuple[str, str, str, str]] = {
    "institutions": ("app.services.campus_service", "app.services.campus_service", "import_institution", "app.schemas.campus:InstitutionImportIn"),
    "assessment_items": ("app.services.assessments_service", "app.services.assessments.item_import", "import_assessment_item", "app.schemas.assessments:AssessmentItemImportIn"),
    "workforce_plan_lines": ("app.services.workforce_service", "app.services.workforce.imports", "import_plan_line", "app.schemas.workforce:PlanLineImportIn"),
    "projects": ("app.services.projects_service", "app.services.projects.imports", "import_project", "app.schemas.projects:ProjectImportIn"),
    "project_roles": ("app.services.projects_service", "app.services.projects.imports", "import_project_role", "app.schemas.projects:ProjectRoleImportIn"),
    "project_assignments": ("app.services.projects_service", "app.services.projects.imports", "import_project_assignment", "app.schemas.projects:ProjectAssignmentImportIn"),
    "immigration_cases": ("app.services.immigration_service", "app.services.immigration.service", "import_immigration_case", "app.schemas.immigration:ImmigrationCaseImportIn"),
    "referral_history": ("app.services.referrals_service", "app.services.referrals.imports", "import_referral", "app.schemas.referrals:ReferralImportIn"),
}  # fmt: skip

# kind -> {column whose "unknown" error means the depends_on kind is not applied yet: that kind}
DEPENDENCY_COLUMNS: dict[str, dict[str, str]] = {
    "project_roles": {"project_human_id": "projects"},
    "project_assignments": {
        "project_human_id": "projects",
        "role_title": "project_roles",
        "placement_human_id": "placements",
    },
}

_ID = "00000000-0000-0000-0000-000000000000"

P3_KINDS: dict[str, KindDef] = {
    "institutions": KindDef("institutions", "Institutions", "Epic 43", (), (
        C("name", True, "Institution name (unique)", "Anna University"),
        C("city", False, "City", "Chennai"),
        C("state", False, "State", "Tamil Nadu"),
        C("country", False, "ISO country code (blank = IN)", "IN"),
        C("tier", False, "tier_1 | tier_2 | tier_3 | other", "tier_1"),
        C("programs", False, "Programs: Program:Branch1|Branch2;...", "B.E.:CSE|ECE;MCA:"),
        C("website", False, "Website", "https://www.annauniv.edu"),
        C("contact_name", False, "Placement officer name (TPO; required with any contact_* column)", "R. Kumar"),
        C("contact_title", False, "Placement officer title", "Training & Placement Officer"),
        C("contact_email", False, "Placement officer email", "tpo@annauniv.edu"),
        C("contact_phone", False, "Placement officer phone", "+919876543210"),
    )),
    "assessment_items": KindDef("assessment_items", "Assessment items", "Epic 40", (), (
        C("kind", True, "single | multi | true_false | numeric | short_text | code_* | essay", "single"),
        C("stem", True, "Question text", "Which keyword declares a constant in Java?"),
        C("options", False, "Choices separated by ';' (choice kinds) or lines (code_order)", "final;const;static"),
        C("answer", False, "Answer key (per kind, e.g. the 1-based choice number)", "1"),
        C("points", False, "Points (decimal string, blank = 1)", "1"),
        C("difficulty", False, "1-5 (blank = 3)", "2"),
        C("tags", False, "Tags separated by ';'", "java;basics"),
        C("skill_keys", False, "Skill keys separated by ';' (global:<uuid> / tenant:<uuid>)", ""),
        C("language", False, "Code language (code kinds)", ""),
        C("code", False, "Code snippet (code kinds)", ""),
        C("rubric", False, "Rubric criteria label:max_points separated by ';' (essay / freeform)", ""),
    )),
    "workforce_plan_lines": KindDef("workforce_plan_lines", "Workforce plan lines", "Epic 44", (), (
        C("plan_name", True, "Name of an editable Workforce Plan", "FY27 Delivery plan"),
        C("role", True, "Role title", "Java Developer"),
        C("skills", False, "Skills with minimum proficiency: 'Java:4; Spring:3'", "Java:4; Spring:3"),
        C("headcount", True, "Headcount", "3"),
        C("type", False, "Line type (blank = the plan default)", "contract"),
        C("location", False, "Location", "Bengaluru"),
        C("start_month", True, "Start month (YYYY-MM)", "2026-11"),
        C("budget", False, "Budget (decimal string)", "1500000.00"),
        C("bill_rate", False, "Bill rate (decimal string)", "2500.00"),
        C("unit", False, "Rate unit (hour | day | month)", "hour"),
        C("notes", False, "Notes", ""),
    )),
    "projects": KindDef("projects", "Projects", "Epic 45", (), (
        C("name", True, "Project name", "Core banking migration"),
        C("client_name", True, "Client name (exact)", "Acme Bank"),
        C("sow_human_id", False, "SOW contract id (CTR-...) of the client", ""),
        C("owner_email", False, "Owner (User email)", ""),
        C("start_date", False, "Start date (yyyy-mm-dd)", "2026-04-01"),
        C("end_date", False, "End date (yyyy-mm-dd)", "2027-03-31"),
        C("status", False, "draft | active | on_hold | completed (non-draft needs an Active SOW)", "draft"),
        C("description", False, "Description", ""),
    )),
    "project_roles": KindDef("project_roles", "Project roles", "Epic 45", ("projects",), (
        C("project_human_id", True, "Project id (PRJ-...)", "PRJ-000001"),
        C("title", True, "Role title", "Senior Java Developer"),
        C("headcount", False, "Headcount (blank = 1)", "2"),
        C("start_date", False, "Start date (yyyy-mm-dd)", ""),
        C("end_date", False, "End date (yyyy-mm-dd)", ""),
    )),
    "project_assignments": KindDef("project_assignments", "Project assignments", "Epic 45", ("project_roles", "placements"), (
        C("project_human_id", True, "Project id (PRJ-...)", "PRJ-000001"),
        C("role_title", True, "Role title within the project", "Senior Java Developer"),
        C("placement_human_id", True, "Placement id (PLC-...)", "PLC-000001"),
        C("allocation_pct", False, "Allocation percent (blank = 100)", "100"),
        C("start_date", True, "Start date (yyyy-mm-dd)", "2026-04-01"),
        C("end_date", False, "End date (yyyy-mm-dd)", ""),
    )),
    "immigration_cases": KindDef("immigration_cases", "Immigration cases", "Epic 46", ("workers",), (
        C("worker_email", False, "Email of an imported / existing Worker (or give candidate_id)", "asha.rao@example.com"),
        C("candidate_id", False, "Candidate id (instead of worker_email)", ""),
        C("visa_type_key", True, "Visa type key", "uk_skilled_worker"),
        C("stage_key", False, "Current stage key of the visa type", ""),
        C("owner_user_id", False, "Coordinator User id", ""),
        C("placement_id", False, "Placement id", ""),
        C("receipt_number", False, "Filing receipt number (stored encrypted)", "", sensitive=True),
        C("permit_type", False, "Permit type (with a permit number)", ""),
        C("permit_number", False, "Permit number (stored encrypted; the case is recorded Approved)", "", sensitive=True),
        C("permit_valid_from", False, "Permit valid from (yyyy-mm-dd)", ""),
        C("permit_valid_to", False, "Permit valid to (yyyy-mm-dd)", ""),
    )),
    "referral_history": KindDef("referral_history", "Referral history", "Epic 42", (), (
        C("referrer_email", True, "Referrer (User or Worker) email", "emp@example.com"),
        C("candidate_email", True, "Referred candidate email", "friend@example.com"),
        C("demand_req_id", False, "Demand id (REQ-...)", ""),
        C("submitted_on", True, "Submitted on (yyyy-mm-dd)", "2026-01-15"),
        C("status", True, "Referral status", "hired"),
        C("reward_status", False, "Reward status", ""),
    )),
}  # fmt: skip


def _dto(kind: str) -> type[BaseModel] | None:
    import importlib

    path = P3_OWNERS[kind][3]
    module, _, name = path.partition(":")
    try:
        return getattr(importlib.import_module(module), name, None)  # type: ignore[no-any-return]
    except ImportError:
        return None


def make_handler(kind: str) -> Any:
    deps = DEPENDENCY_COLUMNS.get(kind, {})

    async def handler(
        ctx: RequestContext,
        db: AsyncSession,
        row: dict[str, str],
        batch_id: uuid.UUID,  # ruff: ignore[unused-function-argument]
        *,
        validate_only: bool,
        scratch: dict[str, Any],  # ruff: ignore[unused-function-argument]
    ) -> str | None:
        fn = owners.owner(kind)
        model = _dto(kind)
        assert fn is not None and model is not None
        values: dict[str, Any] = {k: v.strip() for k, v in row.items() if k and v is not None and v.strip()}
        if kind == "immigration_cases":
            email = values.pop("worker_email", None)
            if "candidate_id" not in values:
                if email is None:
                    raise owners.RowErrors(
                        [owners.err("worker_email", "required", "Give worker_email or candidate_id")]
                    )
                cid = await owners.worker_id(ctx, db, email)
                if cid is None:
                    raise owners.WorkerPending
                values["candidate_id"] = str(cid)
        try:
            data = model.model_validate(values)
        except ValidationError as exc:
            raise owners.RowErrors(owners.from_pydantic(exc)) from exc
        try:
            out = await fn(ctx, db, data, validate_only=validate_only)
        except DomainError as exc:
            errors = owners.from_domain(exc)
            column = errors[0]["column"] if errors else None
            if column in deps and str(errors[0].get("message", "")).lower().startswith("unknown"):
                raise owners.DependencyPending(deps[column], column) from exc
            raise owners.RowErrors(errors) from exc
        if validate_only:
            return None
        ref = getattr(out, "id", out)
        return f"{kind}:{ref}" if ref else None

    return handler


def register() -> None:
    """Additive registration into the go-live registries (idempotent)."""
    for kind, definition in P3_KINDS.items():
        templates.KINDS.setdefault(kind, definition)
        owners.EXTRA_OWNERS.setdefault(kind, P3_OWNERS[kind][:3])
        owners.HANDLERS.setdefault(kind, make_handler(kind))


register()
