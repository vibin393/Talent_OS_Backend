# ruff: file-ignore[line-too-long, ambiguous-unicode-character-string]
"""Auditor pack (Epic 49 A7, FR-248): a PDF (``documents/render.py``, template ``fairness_auditor_pack``) and one CSV
per table (the P1 export writer's ``csv_line``, formula-safe), stored under the tenant storage prefix and delivered as
expiring, permission-checked download links (``GET /fairness/runs/{id}/pack/files/{name}``, ``fairness:read``).

Contents: the AEDT entry, the selection point definition, the date range, the data description (sources, sample size,
unknown counts, category set version, "race/ethnicity not collected; gender reported in the sex position"), the method
(selection / scoring rate, median, k, suppression rule) and the aggregate tables exactly as in the snapshot.
"""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, storage
from app.core.context import RequestContext
from app.core.errors import Gone
from app.schemas.fairness import PackFileOut, PackIn, PackOut
from app.services.fairness import inventory, reports, selection_points
from app.services.fairness.common import conflict, not_found, now, tenant_of

TEMPLATE = "fairness_auditor_pack"
LINK_TTL = timedelta(days=7)
NOT_COLLECTED = "Race/ethnicity is not collected; gender is reported in the LL144 'sex' position."
SUPPRESSION_RULE = (
    "A cell is suppressed when it has fewer than k applicants, or fewer than k selected or not selected people "
    "(and, for score-based tools, fewer than k above or not above the median). Complementary cells are suppressed "
    "until every published total contains zero or at least two suppressed cells. The unknown count is always shown."
)
POINT_LABELS = {
    "stage_transition": "Stage transition",
    "ai_match_threshold": "AI Match threshold",
    "ai_screening_pass": "AI Screening pass",
    "assessment_pass": "Assessment pass",
    # Epic 65: Phase 4 agent selection points.
    "agent_shortlist_added": "Agent shortlist → added to pipeline",
    "agent_screening_flag_advanced": "Agent screening flag → advanced",
    "agent_draft_rating_accepted": "Interview Assistant draft rating accepted",
}
COLUMNS = (
    ("label", "Category"),
    ("applicants", "Applicants"),
    ("selected", "Selected"),
    ("rate", "Selection rate"),
    ("impact_ratio", "Impact ratio"),
    ("flag", "Below four-fifths"),
)
SCORE_COLUMNS = (("scoring_rate", "Scoring rate"), ("scoring_impact_ratio", "Scoring impact ratio"))


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _columns(snapshot: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    return COLUMNS + (SCORE_COLUMNS if snapshot.get("median_score") is not None else ())


def markdown(run: Any, aedt: Any, generated_at: Any) -> str:
    snap = run.snapshot or {}
    cols = _columns(snap)
    unknown = snap.get("unknown_counts") or {}
    lines = [
        "# Adverse-impact audit pack",
        "",
        f"Run {run.id} -- generated {generated_at:%Y-%m-%d %H:%M} UTC",
        "",
        "## Automated employment decision tool",
        "",
        f"- Name: {aedt.name}",
        f"- Feature: {aedt.feature}",
        f"- Version: {aedt.version or '-'}",
        f"- First used on: {aedt.first_used_on or '-'}",
        f"- Data used: {aedt.data_used or '-'}",
        f"- Job categories: {', '.join(aedt.job_categories or []) or 'All'}",
        "",
        "## Selection point",
        "",
        f"- {POINT_LABELS.get(run.selection_point, run.selection_point)}: {selection_points.DEFINITIONS.get(run.selection_point, '')}",
        f"- Parameters: {', '.join(f'{k}={v}' for k, v in (run.params or {}).items()) or 'none'}",
        f"- Date range: {run.date_from} to {run.date_to}",
        "",
        "## Data description",
        "",
        "- Sources: Talent OS applications, stage history, AI Match scores, AI screenings and assessment results; voluntary candidate self-identification (gender, age band, disability).",
        f"- Sample size: {run.total_applicants or 0}",
        f"- Unknown counts: {', '.join(f'{k}={v}' for k, v in unknown.items()) or '-'}",
        f"- Category set version: {snap.get('category_set_version', '-')}",
        f"- {NOT_COLLECTED}",
        "",
        "## Method",
        "",
        "- Selection rate = selected / applicants per category; impact ratio = category rate / highest shown category rate; flagged below 0.8 (four-fifths).",
        f"- Scoring rate (score-based tools) = share scoring above the sample median (median {snap.get('median_score') or '-'}).",
        f"- k = {run.k}. {SUPPRESSION_RULE}",
        "",
    ]
    for table in snap.get("tables") or []:
        lines += [
            f"## {table['key'].replace('_', ' ').title()}",
            "",
            "| " + " | ".join(h for _, h in cols) + " |",
            "|" + "---|" * len(cols),
        ]
        for row in table["rows"]:
            lines.append("| " + " | ".join(_fmt(row.get(key)) for key, _ in cols) + " |")
        lines += [
            "",
            f"Totals: {table['totals']['applicants']} applicants, {table['totals']['selected']} selected.",
            "",
        ]
    return "\n".join(lines)


def csv_for(table: dict[str, Any], snapshot: dict[str, Any]) -> bytes:
    from app.services.reporting.export import csv_line

    cols = _columns(snapshot)
    out = [csv_line([h for _, h in cols])]
    out += [csv_line([_fmt(row.get(key)) for key, _ in cols]) for row in table["rows"]]
    out.append(csv_line(["Totals", table["totals"]["applicants"], table["totals"]["selected"]]))
    return ("﻿" + "".join(out)).encode("utf-8")


def _name(key: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in key.replace("×", "_x_"))


async def generate(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID, data: PackIn) -> PackOut:  # ruff: ignore[unused-function-argument]
    from app.services.documents.render import render_markdown

    run = await reports.load(db, run_id)
    if run.status != "completed" or not run.snapshot:
        raise conflict("fairness_run_not_completed", "The report has not completed yet")
    aedt = await inventory.load(db, data.aedt_id)
    tenant_id = tenant_of(db)
    at = now()
    store = storage.get_storage()
    files: list[dict[str, Any]] = []
    pdf = render_markdown(
        markdown(run, aedt, at), header="Adverse-impact audit pack", footer=TEMPLATE, title="Auditor pack"
    )
    key = storage.new_key(tenant_id, at)
    await store.put(key, pdf, "application/pdf")
    files.append({"name": "auditor-pack.pdf", "kind": "pdf", "key": key})
    for table in run.snapshot.get("tables") or []:
        key = storage.new_key(tenant_id, at)
        await store.put(key, csv_for(table, run.snapshot), "text/csv")
        files.append({"name": f"{_name(table['key'])}.csv", "kind": "csv", "key": key})
    run.pack = {
        "run_id": str(run.id), "aedt_id": str(aedt.id), "generated_at": at.isoformat(),
        "expires_at": (at + LINK_TTL).isoformat(), "files": files,
    }  # fmt: skip
    await db.flush()
    audit.record(
        "fairness.pack_exported", "fairness_run", run.id, session=db, aedt_id=str(aedt.id), files=len(files)
    )
    return pack_out(run)


def pack_out(run: Any) -> PackOut:
    p = run.pack or {}
    return PackOut(
        run_id=run.id,
        aedt_id=uuid.UUID(p["aedt_id"]),
        generated_at=p["generated_at"],
        expires_at=p["expires_at"],
        files=[
            PackFileOut(
                name=f["name"], kind=f["kind"], url=f"/api/v1/fairness/runs/{run.id}/pack/files/{f['name']}"
            )
            for f in p.get("files") or []
        ],
    )


async def download(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    run_id: uuid.UUID,
    name: str,
) -> tuple[bytes, str, str]:
    from datetime import datetime

    run = await reports.load(db, run_id)
    p = run.pack or {}
    entry = next((f for f in p.get("files") or [] if f["name"] == name), None)
    if entry is None:
        raise not_found("Pack file")
    if datetime.fromisoformat(p["expires_at"]) <= now():
        raise Gone("This download link has expired; generate the pack again", code="fairness_pack_expired")
    data = await storage.get_storage().get(entry["key"])
    audit.record("fairness.pack_downloaded", "fairness_run", run.id, session=db, file=name)
    return data, ("application/pdf" if entry["kind"] == "pdf" else "text/csv"), name
