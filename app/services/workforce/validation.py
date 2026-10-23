# ruff: file-ignore[non-imperative-mood, line-too-long]
"""Plan line validation (Epic 44, FR-222): pure parsers plus the catalogue / project loaders.

A line is validated from a normalised ``dict`` (the API's ``LineIn`` dump or an import row parsed by
``parse_import_row``) and returns ``(values, errors)``: ``values`` are ready for ``WorkforcePlanLine`` columns and
``errors`` is a list of ``(field, message)``. Rules: role title required; skills resolve against the P1 / Epic 37
skill catalogue (``skill_key`` or a name; unknown -> error, unless ``allow_unmatched`` for Client forecasts);
``min_proficiency`` 1..5; Money is ``Decimal`` with at most 2 dp (a float is refused); the planned start month lies
inside the plan period; ``project_role_id`` belongs to the plan's project (``projects_service.project_context``);
headcount 1..``max_headcount_per_line`` (<= 500).
"""

import calendar
import re
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.skill_refs import SkillKey
from app.models.workforce import LINE_RATE_UNITS, LINE_TYPES, WORK_MODES, WorkforcePlan
from app.schemas.workforce import parse_money, parse_month
from app.services.workforce.common import invalid, system_ctx, tenant_of

Errors = list[tuple[str, str]]
MAX_HEADCOUNT = 500
_SKILL_RE = re.compile(r"^\s*(?P<name>[^:]+?)\s*(?::\s*(?P<level>[^:]*?)\s*)?$")


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


# ================================================================ pure parsers (import rows)
def parse_skills_text(text: str | None) -> tuple[list[dict[str, Any]], Errors]:
    """``"Java:4; Spring:3"`` -> ``[{name, min_proficiency}]`` (``;`` or ``,`` separated; level optional)."""
    out: list[dict[str, Any]] = []
    errors: Errors = []
    if not text or not text.strip():
        return out, errors
    for part in re.split(r"[;,]", text):
        if not part.strip():
            continue
        match = _SKILL_RE.match(part)
        if match is None:
            errors.append(("skills", f"Unreadable skill '{part.strip()}'"))
            continue
        level_raw = (match.group("level") or "").strip()
        level: int | None = None
        if level_raw:
            if not level_raw.isdigit() or not 1 <= int(level_raw) <= 5:
                errors.append(("skills", f"Proficiency of '{match.group('name').strip()}' must be 1-5"))
                continue
            level = int(level_raw)
        out.append({"skill_key": None, "name": match.group("name").strip()[:200], "min_proficiency": level})
    return out, errors


_TYPE_ALIASES = {
    "": "new",
    "new": "new",
    "replacement": "replacement",
    "backfill": "replacement",
    "project": "project",
    "recurring": "recurring",
}
_UNIT_ALIASES = {
    "hour": "hour",
    "hourly": "hour",
    "day": "day",
    "daily": "day",
    "month": "month",
    "monthly": "month",
    "annual": "annual",
    "year": "annual",
    "yearly": "annual",
}


def parse_import_row(row: dict[str, Any]) -> tuple[dict[str, Any], Errors]:
    """A CSV / XLSX / go-live row (column names of ``PlanLineImportIn``) -> the normalised line dict."""
    errors: Errors = []
    data: dict[str, Any] = {}
    data["role_title"] = str(row.get("role") or "").strip()
    skills, skill_errors = parse_skills_text(str(row.get("skills") or ""))
    data["skills"] = skills
    errors += skill_errors
    raw_hc = str(row.get("headcount") if row.get("headcount") is not None else "").strip()
    if raw_hc.endswith(".0"):  # XLSX integer cells read back as "3.0"
        raw_hc = raw_hc[:-2]
    if not raw_hc.isdigit():
        errors.append(("headcount", "Headcount must be a whole number"))
        data["headcount"] = None
    else:
        data["headcount"] = int(raw_hc)
    line_type = _TYPE_ALIASES.get(str(row.get("type") or "").strip().lower())
    if line_type is None:
        errors.append(("type", "Type must be new, replacement, project or recurring"))
    data["line_type"] = line_type or "new"
    data["location_text"] = str(row.get("location") or "").strip() or None
    raw_month = row.get("start_month")
    try:
        data["start_month"] = parse_month(raw_month)
    except ValueError:
        errors.append(("start_month", "Start month must be YYYY-MM"))
        data["start_month"] = None
    for src, dst in (("budget", "budget_amount"), ("bill_rate", "bill_rate_amount")):
        value = row.get(src)
        try:
            data[dst] = parse_money(value.strip() if isinstance(value, str) else value)
        except ValueError as exc:
            errors.append((src, str(exc)))
            data[dst] = None
    unit_raw = str(row.get("unit") or "").strip().lower()
    unit = _UNIT_ALIASES.get(unit_raw) if unit_raw else None
    if unit_raw and unit is None:
        errors.append(("unit", "Unit must be hour, day, month or annual"))
    data["rate_unit"] = unit
    data["notes"] = str(row.get("notes") or "").strip() or None
    return data, errors


# ================================================================ line validation (DB loaders)
async def resolve_skills(
    ctx: RequestContext, db: AsyncSession, skills: list[dict[str, Any]], *, allow_unmatched: bool = False
) -> tuple[list[dict[str, Any]], Errors]:
    from app.schemas.candidates import SkillRefIn
    from app.services.candidates.skills import get_ref, resolve_skill

    out: list[dict[str, Any]] = []
    errors: Errors = []
    seen: set[str] = set()
    for i, s in enumerate(skills):
        level = s.get("min_proficiency")
        if level is not None and (
            isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 5
        ):
            errors.append((f"skills.{i}.min_proficiency", "Minimum proficiency must be 1-5"))
            continue
        key_text = s.get("skill_key")
        name = (s.get("name") or "").strip()
        if key_text:
            try:
                key = SkillKey.parse(str(key_text))
            except ValueError:
                errors.append((f"skills.{i}", "Unknown skill"))
                continue
            ref = await get_ref(db, SkillRefIn(kind=key.kind, id=key.id))
            if ref is None:
                errors.append((f"skills.{i}", "Unknown skill"))
                continue
            entry = {"skill_key": key.as_str(), "name": name or ref.name, "min_proficiency": level}
        else:
            ref = await resolve_skill(ctx, db, name, create_pending=False) if name else None
            if ref is None or ref.status == "rejected":
                if allow_unmatched and name:
                    entry = {"skill_key": None, "name": name[:200], "min_proficiency": level}
                    if name.lower() not in seen:
                        seen.add(name.lower())
                        out.append(entry)
                    continue
                errors.append((f"skills.{i}", f"Unknown skill '{name}'"))
                continue
            entry = {
                "skill_key": SkillKey(kind=ref.kind, id=ref.id).as_str(),
                "name": ref.name,
                "min_proficiency": level,
            }
        if entry["skill_key"] in seen:
            continue
        seen.add(entry["skill_key"])  # type: ignore[arg-type]
        out.append(entry)
    return out, errors


async def _project_roles(
    ctx: RequestContext, db: AsyncSession, project_id: uuid.UUID
) -> set[uuid.UUID] | None:
    from app.services import projects_service

    try:
        context = await projects_service.project_context(
            system_ctx(ctx.tenant_id or tenant_of(db)), db, project_id
        )
    except NotFound:
        return None
    return {r.id for r in context.roles}


async def validate_line(
    ctx: RequestContext,
    db: AsyncSession,
    plan: WorkforcePlan,
    data: dict[str, Any],
    *,
    max_headcount: int = MAX_HEADCOUNT,
    allow_unmatched_skills: bool = False,
    partial: bool = False,
) -> tuple[dict[str, Any], Errors]:
    """Validate a (possibly partial) line dict against ``plan``; returns column values and errors."""
    errors: Errors = []
    values: dict[str, Any] = {}

    def has(key: str) -> bool:
        return not partial or key in data

    if has("role_title"):
        title = (data.get("role_title") or "").strip()
        if not title:
            errors.append(("role_title", "Role is required"))
        values["role_title"] = title[:200]
    if has("headcount"):
        hc = data.get("headcount")
        cap = min(max_headcount, MAX_HEADCOUNT)
        if not isinstance(hc, int) or isinstance(hc, bool) or hc < 1:
            if hc is not None or not partial:
                errors.append(("headcount", "Headcount must be at least 1"))
        elif hc > cap:
            errors.append(("headcount", f"Headcount must be at most {cap} per line"))
        else:
            values["headcount"] = hc
    if has("line_type"):
        lt = data.get("line_type") or "new"
        if lt not in LINE_TYPES:
            errors.append(("line_type", "Unknown line type"))
        values["line_type"] = lt
    if has("work_mode"):
        wm = data.get("work_mode")
        if wm is not None and wm not in WORK_MODES:
            errors.append(("work_mode", "Unknown work mode"))
        values["work_mode"] = wm
    if has("rate_unit"):
        ru = data.get("rate_unit")
        if ru is not None and ru not in LINE_RATE_UNITS:
            errors.append(("rate_unit", "Unknown rate unit"))
        values["rate_unit"] = ru
    if has("location_text"):
        values["location_text"] = data.get("location_text")
    if has("notes"):
        values["notes"] = data.get("notes")
    for key in ("budget_amount", "bill_rate_amount"):
        if has(key):
            raw = data.get(key)
            try:
                values[key] = raw if isinstance(raw, Decimal) else parse_money(raw)
            except ValueError as exc:
                errors.append((key, str(exc)))
    if has("skills"):
        skills, skill_errors = await resolve_skills(
            ctx, db, list(data.get("skills") or []), allow_unmatched=allow_unmatched_skills
        )
        values["skills"] = skills
        errors += skill_errors
    if has("start_month"):
        start = data.get("start_month")
        if not isinstance(start, date):
            errors.append(("start_month", "Start month must be YYYY-MM"))
        else:
            start = start.replace(day=1)
            if start < plan.period_start.replace(day=1) or start > plan.period_end:
                errors.append(("start_month", "The start month must be inside the plan period"))
            values["period_start"] = start
    if has("end_month") or not partial:
        end_raw = data.get("end_month")
        start = values.get("period_start") or data.get("_current_start")
        end = month_end(end_raw) if isinstance(end_raw, date) else plan.period_end
        if end > plan.period_end:
            end = plan.period_end
        if isinstance(start, date) and end < start:
            errors.append(("end_month", "The end month must be on or after the start month"))
        if has("end_month") or not partial:
            values["period_end"] = end
    if has("project_role_id"):
        role_id = data.get("project_role_id")
        if role_id is not None:
            if plan.project_id is None:
                errors.append(("project_role_id", "The plan has no project"))
            else:
                roles = await _project_roles(ctx, db, plan.project_id)
                if roles is None or role_id not in roles:
                    errors.append(("project_role_id", "The role does not belong to the plan's project"))
        values["project_role_id"] = role_id
    return values, errors


def raise_errors(errors: Errors) -> None:
    if errors:
        raise invalid("line_invalid", errors[0][1], fields=[{"field": f, "message": m} for f, m in errors])
