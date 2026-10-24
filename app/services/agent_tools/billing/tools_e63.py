# ruff: file-ignore[line-too-long]
"""Epic 63 Billing Agent tools (``owner_epic="63"``, ``version=1``). Issuing, cancelling, crediting or sending an
invoice, assigning numbers, editing lines or rates and recording payments are ★ and have no tool (``billing.issue`` /
``billing.notes`` / ``billing.einvoice`` are never imported).

| Key | Risk | Notes |
|---|---|---|
| ``billing.preview_run`` | read | what a run would contain now, per client, with exceptions (nothing is written) |
| ``billing.check_rates`` | read | each placement's bill rate and rate-card status (no rate check is recorded) |
| ``billing.list_unapproved_time`` | read | timesheets of the period not approved yet |
| ``billing.prepare_run_draft`` | **high** (suggest) | a Proposal; on approval P2 ``create_run`` runs AS THE APPROVING Finance User (drafts only, no numbers) |
| ``billing.flag_exception`` | low (auto) | an internal note on the Client listing one exception kind |

Change-a-draft-line / rate Proposals are not offered in Phase 4 (spec Known gaps 4); exceptions are flagged instead.
"""

import uuid
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.agentic import IdempotencyRule, ProposalPreview, ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.core.errors import Forbidden, NotFound
from app.schemas.billing import RunCreate, RunOut, RunPreviewOut
from app.services import agent_runtime_service as runtime

OWNER_EPIC = "63"
VERSION = 1
ExceptionKind = Literal["po_missing", "po_cap_breach", "rate_mismatch", "unapproved_time", "billing_rule_missing", "mixed_currency"]
EXCEPTION_LABELS = {
    "po_missing": "Missing PO",
    "po_cap_breach": "PO cap reached",
    "rate_mismatch": "Rate outside rate card",
    "unapproved_time": "Unapproved time",
    "billing_rule_missing": "No billing rule",
    "mixed_currency": "Mixed currency",
}


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _scoped(tctx: Any) -> Any:
    return runtime.scope_context(tctx.request, tctx.owner_user_id)


async def _entity(tctx: Any, legal_entity_id: uuid.UUID) -> Any:
    from app.models.legal_entities import LegalEntity

    le = await tctx.db.get(LegalEntity, legal_entity_id)
    if le is None:
        raise NotFound("Legal entity not found")
    return le


class PeriodIn(_In):
    legal_entity_id: uuid.UUID
    period_start: date
    period_end: date

    @model_validator(mode="after")
    def _period(self) -> "PeriodIn":
        if self.period_end < self.period_start or (self.period_end - self.period_start).days > 92:
            raise ValueError("period_end must be on or after period_start and within 3 months")
        return self


# ================================================================ billing.preview_run
class PreviewIn(PeriodIn):
    client_id: uuid.UUID | None = None


def _refs(out: RunPreviewOut) -> tuple[tuple[str, uuid.UUID], ...]:
    refs: list[tuple[str, uuid.UUID]] = []
    for c in out.clients:
        refs.append(("client", c.client_id))
        refs += [("placement", e.placement_id) for e in c.exceptions if e.placement_id]
    return tuple(dict.fromkeys(refs))


async def _preview(tctx: Any, args: PreviewIn) -> ToolOutcome:
    from app.services import billing_service

    await _entity(tctx, args.legal_entity_id)
    out: RunPreviewOut = await billing_service.preview_run(
        _scoped(tctx), tctx.db,
        RunCreate(legal_entity_id=args.legal_entity_id, period_start=args.period_start, period_end=args.period_end, client_id=args.client_id),
    )  # fmt: skip
    return ToolOutcome(
        out, refs=_refs(out), target=("legal_entity", args.legal_entity_id),
        summary=f"preview: {out.draft_count} drafts, {out.exception_count} exceptions",
    )


# ================================================================ billing.check_rates
class RatesIn(_In):
    legal_entity_id: uuid.UUID
    placement_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)


class RateRow(_Out):
    placement_id: uuid.UUID
    client_id: uuid.UUID | None
    currency: str | None
    rate_unit: str | None
    bill_rate: str | None
    status: str  # within_card | outside_card | no_rate_card | no_bill_rate | unavailable
    min_bill_rate: str | None = None
    max_bill_rate: str | None = None


class RatesOut(_Out):
    placements: list[RateRow]


async def _rates(tctx: Any, args: RatesIn) -> ToolOutcome:
    from app.models.placements import Placement
    from app.schemas.commercials import RateCheckRequest
    from app.services import commercials_service, placements_service

    await _entity(tctx, args.legal_entity_id)
    rows: list[RateRow] = []
    today = date.today()
    for pid in dict.fromkeys(args.placement_ids):
        p = await tctx.db.get(Placement, pid)
        if p is None or p.legal_entity_id != args.legal_entity_id:
            raise NotFound("Placement not found")
        try:
            bc = await placements_service.billing_context(_scoped(tctx), tctx.db, pid, today)
        except Exception:
            rows.append(RateRow(placement_id=pid, client_id=p.client_id, currency=p.currency, rate_unit=None, bill_rate=None, status="no_bill_rate"))
            continue
        status, lo, hi = "unavailable", None, None
        if bc.bill_rate is not None:
            try:
                res = await commercials_service.validate_rate(
                    _scoped(tctx), tctx.db,
                    RateCheckRequest(
                        subject_kind="placement", subject_id=None, client_id=p.client_id, contract_id=p.contract_id, on_date=today,
                        role=p.role_title, bill_rate=bc.bill_rate, currency=p.currency, rate_unit=bc.rate_unit,
                    ),
                )  # fmt: skip
                status = {"pass": "within_card", "ok": "within_card", "no_card": "no_rate_card"}.get(str(res.status), "outside_card" if res.requires_exception else str(res.status))
                lo = str(res.min_bill_rate) if res.min_bill_rate is not None else None
                hi = str(res.max_bill_rate) if res.max_bill_rate is not None else None
            except Exception:
                status = "unavailable"
        else:
            status = "no_bill_rate"
        rows.append(
            RateRow(
                placement_id=pid, client_id=p.client_id, currency=p.currency, rate_unit=bc.rate_unit,
                bill_rate=str(bc.bill_rate) if bc.bill_rate is not None else None, status=status, min_bill_rate=lo, max_bill_rate=hi,
            )
        )  # fmt: skip
    return ToolOutcome(
        RatesOut(placements=rows), refs=tuple(("placement", r.placement_id) for r in rows),
        target=("legal_entity", args.legal_entity_id), summary=f"{len(rows)} placements checked",
    )


# ================================================================ billing.list_unapproved_time
class UnapprovedRow(_Out):
    timesheet_id: uuid.UUID
    human_id: str
    placement_id: uuid.UUID
    period_start: date
    period_end: date
    status: str


class UnapprovedOut(_Out):
    timesheets: list[UnapprovedRow]


async def _unapproved(tctx: Any, args: PeriodIn) -> ToolOutcome:
    from app.services import time_service

    await _entity(tctx, args.legal_entity_id)
    rows = await time_service.unapproved_timesheets(_scoped(tctx), tctx.db, args.legal_entity_id, args.period_start, args.period_end)
    out = UnapprovedOut(
        timesheets=[
            UnapprovedRow(
                timesheet_id=r.timesheet_id, human_id=r.human_id, placement_id=r.placement_id, period_start=r.period_start,
                period_end=r.period_end, status=r.status,
            )
            for r in rows[:200]
        ]
    )  # fmt: skip
    return ToolOutcome(out, target=("legal_entity", args.legal_entity_id), summary=f"{len(out.timesheets)} timesheets not approved")


# ================================================================ billing.prepare_run_draft
class PrepareIn(PreviewIn):
    gst_registration_id: uuid.UUID | None = None


def _run_create(args: PrepareIn) -> RunCreate:
    return RunCreate(
        legal_entity_id=args.legal_entity_id, gst_registration_id=args.gst_registration_id, period_start=args.period_start,
        period_end=args.period_end, client_id=args.client_id,
    )  # fmt: skip


async def _digest(probe: Any) -> str | None:
    from app.services import billing_service

    out = await billing_service.preview_run(probe.tctx.request, probe.db, _run_create(probe.args))
    return out.digest[:64]


async def _prepare(tctx: Any, args: PrepareIn) -> ToolOutcome:
    from app.services import billing_service

    if tctx.execution == "agent":  # pragma: no cover - high risk never auto-executes (ceiling); defence in depth
        raise Forbidden("A person must approve starting an invoice run")
    await _entity(tctx, args.legal_entity_id)
    run: RunOut = await billing_service.create_run(tctx.request, tctx.db, _run_create(args))
    return ToolOutcome(
        run, refs=(("invoice_run", run.id),), target=("legal_entity", args.legal_entity_id),
        summary=f"draft invoice run {run.human_id} queued",
    )


async def _prepare_preview(tctx: Any, args: PrepareIn) -> ProposalPreview:
    from app.services import billing_service

    le = await _entity(tctx, args.legal_entity_id)
    out: RunPreviewOut = await billing_service.preview_run(_scoped(tctx), tctx.db, _run_create(args))
    period = f"{args.period_start:%b %Y}" if args.period_start.month == args.period_end.month else f"{args.period_start.isoformat()} to {args.period_end.isoformat()}"
    totals = ", ".join(f"{k} {v}" for k, v in sorted(out.totals.items())) or "0"
    name = getattr(le, "legal_name", None) or getattr(le, "name", None) or "the legal entity"
    lines = [
        f"{c.client_name or c.client_id}: {c.drafts} draft(s), {c.currency or ''} {c.total} — "
        + (", ".join(sorted({EXCEPTION_LABELS.get(e.category, e.kind) for e in c.exceptions})) or "no exceptions")
        for c in out.clients
    ]
    return ProposalPreview(
        title=f"Start invoice run for {name}, {period}: {out.draft_count} draft invoices, {totals} — {out.exception_count} exceptions"[:200],
        summary=("Invoices are created as drafts. Numbers are assigned only when a person issues them.\n" + "\n".join(lines))[:4000],
        fields=(
            ("Legal entity", str(name)), ("Period", period), ("Drafts", str(out.draft_count)), ("Total", totals),
            ("Exceptions", str(out.exception_count)),
        ),
        renderer="billing.prepare_run_draft",
    )  # fmt: skip


# ================================================================ billing.flag_exception
class FlagIn(_In):
    client_id: uuid.UUID
    kind: ExceptionKind
    details: str = Field(min_length=1, max_length=500)


class FlagOut(_Out):
    activity_id: uuid.UUID


async def _flag(tctx: Any, args: FlagIn) -> ToolOutcome:
    from app.schemas.activity import ActivityCreate
    from app.services import activity_service, entity_refs
    from app.services.agent_runtime.sanitize import outbound

    if not await entity_refs.can_read(_scoped(tctx), tctx.db, "client", args.client_id):
        raise NotFound("Client not found")
    body = f"AI note (Billing Agent): {EXCEPTION_LABELS[args.kind]} — {outbound(args.details, max_len=500).text}"
    out = await activity_service.add_activity(
        _scoped(tctx), tctx.db, "client", args.client_id, ActivityCreate(kind="note", body=body[:10_000])
    )
    return ToolOutcome(FlagOut(activity_id=out.id), target=("client", args.client_id), summary=f"exception noted: {args.kind}")


async def _flag_preview(tctx: Any, args: FlagIn) -> ProposalPreview:  # ruff: ignore[unused-function-argument]  # NOSONAR
    return ProposalPreview(
        title=f"Note on the client: {EXCEPTION_LABELS[args.kind]}", summary=args.details[:4000],
        fields=(("Exception", EXCEPTION_LABELS[args.kind]),), renderer="billing.flag_exception",
    )


LE_SCOPE = ScopeRule(resource="legal_entity", target_arg="legal_entity_id")

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        key="billing.preview_run", version=VERSION, owner_epic=OWNER_EPIC, title="Preview an invoice run",
        description="Preview what an invoice run for the legal entity and period would contain now: per client the draft count, items, totals and currency, and the exceptions (missing PO, PO cap reached, rate outside the rate card, unapproved time, no billing rule, mixed currency). Nothing is written and no number is assigned.",
        input_model=PreviewIn, output_model=RunPreviewOut, permissions=("billing:read",), risk="read", scope=LE_SCOPE,
        handler=_preview,
    ),
    ToolSpec(
        key="billing.check_rates", version=VERSION, owner_epic=OWNER_EPIC, title="Check placement rates",
        description="Check the current bill rate of up to 50 placements of the legal entity against the client's rate card (within card, outside card, no rate card, no bill rate). Read only; no rate check is recorded.",
        input_model=RatesIn, output_model=RatesOut, permissions=("billing:read",), risk="read", scope=LE_SCOPE, handler=_rates,
    ),
    ToolSpec(
        key="billing.list_unapproved_time", version=VERSION, owner_epic=OWNER_EPIC, title="Unapproved time",
        description="List the timesheets of the legal entity overlapping the period that are not approved yet (they cannot be billed). Read only.",
        input_model=PeriodIn, output_model=UnapprovedOut, permissions=("timesheet:read",), risk="read", scope=LE_SCOPE,
        handler=_unapproved,
    ),
    ToolSpec(
        key="billing.prepare_run_draft", version=VERSION, owner_epic=OWNER_EPIC, title="Start a draft invoice run",
        description="Propose starting the same draft invoice run a Finance User would start for the legal entity and period. A Finance User approves it; then the run creates draft invoices only -- no numbers are assigned and nothing is issued or sent. Exceptions stay listed; nothing is silently excluded. Call it once per run.",
        input_model=PrepareIn, output_model=RunOut, permissions=("billing:prepare", "billing:write"), risk="high",
        scope=ScopeRule(resource="legal_entity", target_arg="legal_entity_id", version=_digest), handler=_prepare,  # type: ignore[arg-type]
        preview=_prepare_preview, default_mode="suggest", max_calls_per_run=1, timeout_s=60,
    ),
    ToolSpec(
        key="billing.flag_exception", version=VERSION, owner_epic=OWNER_EPIC, title="Flag a billing exception",
        description="Add one internal note on a client describing a billing exception kind found in the preview (missing PO, PO cap reached, rate outside rate card, unapproved time, no billing rule, mixed currency). Once per client and kind.",
        input_model=FlagIn, output_model=FlagOut, permissions=("note:write", "billing:read"), risk="low",
        scope=ScopeRule(resource="client", target_arg="client_id"), handler=_flag, preview=_flag_preview, default_mode="auto",
        idempotency=IdempotencyRule(fields=("client_id", "kind"), per_run=True), max_calls_per_run=12,
    ),
)  # fmt: skip


def register() -> None:
    from app.core.agentic.tools import tools

    for spec in TOOLS:
        if spec.key not in tools():
            register_tool(spec)


register()
