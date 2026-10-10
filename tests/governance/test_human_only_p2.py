"""FR-181 governance suite (Epic 35 story 35.3, spec A4): Phase 2 human-only decisions.

There is no central state machine: every domain owns its transition table and guard. ``DECISIONS`` maps each
FR-181 decision to ``(domain module, transition table, (from, to) rows)``, a declaration check (a) and a probe
(b) that drives the domain guard with an automated ``ActorRef`` kind (``ai`` / ``workflow`` / ``system``).

Deviation from the spec: the spec says 409 ``transition_not_allowed``; the codebase uses both 403
``human_only_action`` (``app.core.actors.human_only_action``) and 409 ``transition_not_allowed`` (actor-set
tables such as BGV / placements / onboarding). Both are accepted.

Probes never touch the database: sync guards get ``SimpleNamespace`` stand-ins; async services are called with
``db=None`` because their human-only guard runs before the first query (the one exception, BGV check waive,
gets its loader monkeypatched). Decisions not enforced by any guard yet live in ``PENDING_HUMAN_ONLY`` and are
reported as skips so the gap stays visible.
"""

import dataclasses
import inspect
import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.actors import APPROVER_KINDS, HUMAN_KINDS, ActorRef
from app.core.context import RequestContext
from app.core.errors import DomainError

AUTOMATED = ("ai", "workflow", "system", "agent", "app")  # Epic 53: +agent; Epic 66: +app
ACCEPTED = {("human_only_action", 403), ("transition_not_allowed", 409)}


def _ctx(kind: str) -> RequestContext:
    return RequestContext(tenant_id=uuid.uuid4(), actor_kind=kind)


def _actor(kind: str) -> ActorRef:
    return ActorRef(kind, None)  # type: ignore[arg-type]


def _guard_first(fn: Callable[..., Any], marker: str) -> bool:
    """Check that a ctx-guarded service without a table has its human-only guard in the body."""
    return marker in inspect.getsource(fn)


@dataclasses.dataclass(frozen=True)
class Decision:
    module: str
    table: str
    rows: tuple[tuple[str, str], ...]
    declared: Callable[[], bool]
    probe: Callable[[str], Awaitable[object] | object]
    approvals_backed: bool = False


# ------------------------------------------------------------------ declarations + probes per domain
def _timesheet() -> Decision:
    from app.services.time import timesheets as m

    rows = (
        ("submitted", "approved"),
        ("in_approval", "approved"),
        ("submitted", "rejected"),
        ("in_approval", "rejected"),
    )

    def probe(kind: str) -> None:
        for frm, to in rows:
            m.transition_timesheet(SimpleNamespace(status=frm, lock_version=0), to, _actor(kind))  # type: ignore[arg-type]

    return Decision(
        m.__name__,
        "TIMESHEET_TRANSITIONS",
        rows,
        lambda: all(m.TIMESHEET_TRANSITIONS[r].human_only for r in rows),
        probe,
        True,
    )


def _expense() -> Decision:
    from app.services.expenses import claims as m

    rows = (
        ("submitted", "approved"),
        ("in_approval", "approved"),
        ("submitted", "partially_approved"),
        ("in_approval", "partially_approved"),
        ("submitted", "rejected"),
        ("in_approval", "rejected"),
    )

    def probe(kind: str) -> None:
        for frm, to in rows:
            m.transition_claim(SimpleNamespace(status=frm, lock_version=0), to, _actor(kind))  # type: ignore[arg-type]

    return Decision(
        m.__name__,
        "EXPENSE_TRANSITIONS",
        rows,
        lambda: all(m.EXPENSE_TRANSITIONS[r].human_only for r in rows),
        probe,
        True,
    )


def _leave() -> Decision:
    from app.services.time import leave_requests as m

    rows = (("requested", "approved"), ("requested", "rejected"), ("cancellation_requested", "cancelled"))

    def probe(kind: str) -> None:
        for frm, to in rows:
            m.transition_leave(SimpleNamespace(status=frm), to, _actor(kind))  # type: ignore[arg-type]

    return Decision(
        m.__name__,
        "LEAVE_TRANSITIONS",
        rows,
        lambda: all(m.LEAVE_TRANSITIONS[r].human_only for r in rows),
        probe,
        True,
    )


def _actor_set(
    m: Any,
    table: str,
    rows: tuple[tuple[str, str], ...],
    check: Callable[[str, str, str], None],
    *,
    approvals: bool = False,
    human_only: Any = None,
) -> Decision:
    """Domains whose table maps (from, to) -> allowed actor kinds (+ a ``HUMAN_ONLY`` set)."""
    tbl = getattr(m, table)

    def declared() -> bool:
        ok = all(not (tbl[r] & set(AUTOMATED)) and tbl[r] <= HUMAN_KINDS for r in rows)
        return ok and (human_only is None or all(r in human_only for r in rows))

    def probe(kind: str) -> None:
        for frm, to in rows:
            check(frm, to, kind)

    return Decision(m.__name__, table, rows, declared, probe, approvals)


def _extension() -> Decision:
    from app.services.placements import extensions as m

    rows = (("pending_client_confirmation", "confirmed"),)
    return _actor_set(m, "TRANSITIONS", rows, m.check_transition, approvals=True, human_only=m.HUMAN_ONLY)


def _bgv_adjudicate() -> Decision:
    from app.services.bgv import status as m

    rows = (("under_review", "clear"), ("under_review", "failed"), ("discrepancy", "failed"))
    return _actor_set(m, "TRANSITIONS", rows, m.check_transition, human_only=m.HUMAN_ONLY)


def _bgv_waive() -> Decision:
    from app.services.bgv import checks as m

    async def probe(kind: str) -> None:
        async def fake_get_check(*_a: Any, **_k: Any) -> Any:
            return SimpleNamespace(status="pending", case_id=uuid.uuid4())

        mp = pytest.MonkeyPatch()
        mp.setattr(m, "get_check", fake_get_check)
        try:
            await m.waive(_ctx(kind), None, uuid.uuid4(), SimpleNamespace(reason="x"))  # type: ignore[arg-type]
        finally:
            mp.undo()

    rows = (("pending", "waived"), ("in_progress", "waived"), ("unable_to_verify", "waived"))
    return Decision(
        m.__name__,
        "(guard in waive: who.kind != 'user')",
        rows,
        lambda: _guard_first(m.waive, 'who.kind != "user"'),
        probe,
    )


def _onboarding_waive() -> Decision:
    from app.services.onboarding import status as m

    rows = tuple(k for k in m.TASK_TRANSITIONS if k[1] == "waived")

    def declared() -> bool:
        return "waived" in m.HUMAN_ONLY_TASK_TARGETS and all(
            not (m.TASK_TRANSITIONS[r] & set(AUTOMATED)) for r in rows
        )

    def probe(kind: str) -> None:
        for frm, to in rows:
            m.check_task(frm, to, kind)

    return Decision(m.__name__, "TASK_TRANSITIONS", rows, declared, probe)


def _offboarding_close() -> Decision:
    from app.services.placements import offboarding as m

    return _actor_set(
        m, "TRANSITIONS", (("ready_to_close", "closed"),), m.check_transition, human_only=m.HUMAN_ONLY
    )


def _placement_joined() -> Decision:
    from app.services.placements import status as m

    rows = (("pre_joining", "active"), ("joining_confirmed", "active"))
    return _actor_set(m, "TRANSITIONS", rows, m.check_transition, human_only=m.HUMAN_ONLY)


def _approvals_only(module: str, subject_attr: str) -> Callable[[], Decision]:
    """Decisions taken only through an Approval Request: the guard is ``approvals.service.decide``."""

    def build() -> Decision:
        import importlib

        m = importlib.import_module(module)
        return Decision(
            module,
            f"approvals subject {getattr(m, subject_attr)!r}",
            (("pending", "approved"), ("pending", "rejected")),
            lambda: True,
            _decide,
            True,
        )

    return build


async def _decide(kind: str) -> None:
    from app.services.approvals import service

    await service.decide(_ctx(kind), None, uuid.uuid4(), None, actor=_actor(kind))  # type: ignore[arg-type]


def _ctx_service(
    module: str,
    fn: str,
    marker: str,
    rows: tuple[tuple[str, str], ...],
    call: Callable[[Any, str], Awaitable[object]],
    *,
    approvals: bool = False,
) -> Callable[[], Decision]:
    def build() -> Decision:
        import importlib

        m = importlib.import_module(module)
        f = getattr(m, fn)
        return Decision(
            module,
            f"(guard in {fn}: {marker})",
            rows,
            lambda: _guard_first(f, marker),
            lambda kind: call(f, kind),
            approvals,
        )

    return build


_ID = uuid.uuid4


DECISIONS: dict[str, Callable[[], Decision]] = {
    "approve/reject Timesheet": _timesheet,
    "approve/reject Expense Claim": _expense,
    "approve/reject Leave Request": _leave,
    "approve/reject Extension": _extension,
    "adjudicate BGV": _bgv_adjudicate,
    "waive BGV Check": _bgv_waive,
    "waive Onboarding Task": _onboarding_waive,
    "close Offboarding Case": _offboarding_close,
    "Placement Joined": _placement_joined,
    "start-gate override": _approvals_only("app.services.placements.gates", "OVERRIDE_SUBJECT"),
    "issue Invoice": _ctx_service(
        "app.services.billing.issue",
        "issue_invoice",
        "require_human(actor_from_ctx(ctx))",
        (("draft", "issued"),),
        lambda f, k: f(_ctx(k), None, _ID()),
        approvals=True,
    ),
    "cancel Invoice": _ctx_service(
        "app.services.billing.einvoice",
        "cancel_irn",
        "require_human(actor_from_ctx(ctx))",
        (("issued", "cancelled"),),
        lambda f, k: f(_ctx(k), None, _ID(), None),
    ),
    "issue Credit Note": _ctx_service(
        "app.services.billing.notes",
        "issue_note",
        "require_human(actor_from_ctx(ctx))",
        (("draft", "issued"),),
        lambda f, k: f(_ctx(k), None, "credit_note", _ID()),
    ),
    "issue Debit Note": _ctx_service(
        "app.services.billing.notes",
        "issue_note",
        "require_human(actor_from_ctx(ctx))",
        (("draft", "issued"),),
        lambda f, k: f(_ctx(k), None, "debit_note", _ID()),
    ),
    "reverse Payment": _ctx_service(
        "app.services.receivables.payments",
        "reverse",
        "_actor_check(ctx)",
        (("recorded", "reversed"),),
        lambda f, k: f(_ctx(k), None, _ID(), None),
    ),
    "write-off": _approvals_only("app.services.receivables.write_offs", "SUBJECT"),
    "close Accounting Period (legal entity)": _ctx_service(
        "app.services.legal_entities.periods",
        "close",
        "_require_human(actor)",
        (("open", "closed"),),
        lambda f, k: f(_ctx(k), None, _ID(), 2026, 1, actor=_actor(k)),
    ),
    "reopen Accounting Period (legal entity)": _ctx_service(
        "app.services.legal_entities.periods",
        "reopen",
        "_require_human(actor)",
        (("closed", "open"),),
        lambda f, k: f(_ctx(k), None, _ID(), 2026, 1, actor=_actor(k), reason="x"),
    ),
    "close Accounting Period (receivables)": _ctx_service(
        "app.services.receivables.periods_close",
        "close",
        "_actor(ctx)",
        (("open", "closed"),),
        lambda f, k: f(_ctx(k), None, _ID(), None),
    ),
    "reopen Accounting Period (receivables request)": _ctx_service(
        "app.services.receivables.periods_close",
        "request_reopen",
        "_actor(ctx)",
        (("closed", "open"),),
        lambda f, k: f(_ctx(k), None, _ID(), None),
        approvals=True,
    ),
    "finalize Payroll Export": _ctx_service(
        "app.services.payroll.exports",
        "finalize",
        "require_human(actor_from_ctx(ctx))",
        (("preview", "final"),),
        lambda f, k: f(_ctx(k), None, _ID()),
    ),
    "accept Vendor Submission (review)": _ctx_service(
        "app.services.vendors.review",
        "accept",
        "_require_user(ctx)",
        (("under_review", "accepted"),),
        lambda f, k: f(_ctx(k), None, _ID(), None),
    ),
}


def _vendor_submission_table() -> Decision:
    from app.services.vendors import submissions as m

    rows = (("submitted", "accepted"), ("under_review", "accepted"))

    async def probe(kind: str) -> None:
        for frm, to in rows:
            await m.set_status(_ctx(kind), None, SimpleNamespace(status=frm), to, _actor(kind))  # type: ignore[arg-type]

    return Decision(
        m.__name__, "TRANSITIONS", rows, lambda: all(m.TRANSITIONS[r] <= HUMAN_KINDS for r in rows), probe
    )


DECISIONS["accept Vendor Submission (table)"] = _vendor_submission_table

# Decisions FR-181 names that no guard enforces yet -> visible skips (decision -> reason / owner epic).
PENDING_HUMAN_ONLY: dict[str, str] = {
    "record Payment": (
        "receivables.payments.record has no actor guard (only the workflow catalogue lists "
        "payment->recorded as human-only, so workflow cannot fire it); owner Epic 30 receivables"
    )
}


# ------------------------------------------------------------------ tests
@pytest.mark.parametrize("name", sorted(DECISIONS))
def test_decision_is_declared_human_only(name: str) -> None:
    d = DECISIONS[name]()
    assert d.rows, f"{name}: no transition rows mapped"
    assert d.declared(), f"{name}: {d.module}.{d.table} {d.rows} is not declared human-only"


@pytest.mark.parametrize("kind", AUTOMATED)
@pytest.mark.parametrize("name", sorted(DECISIONS))
async def test_decision_refuses_automated_actor(name: str, kind: str) -> None:
    d = DECISIONS[name]()
    with pytest.raises(DomainError) as info:
        result = d.probe(kind)
        if inspect.isawaitable(result):
            await result
    err = info.value
    assert (err.code, err.status_code) in ACCEPTED, f"{name} ({kind}): got {err.status_code} {err.code}"


@pytest.mark.parametrize("kind", (*AUTOMATED, "api_key"))
async def test_approvals_decide_refuses_non_human(kind: str) -> None:
    with pytest.raises(DomainError) as info:
        await _decide(kind)
    assert (info.value.code, info.value.status_code) in ACCEPTED


def test_approver_kinds_exclude_automated() -> None:
    assert not APPROVER_KINDS & {"ai", "workflow", "system", "api_key", "agent"}


def test_approvals_backed_decisions_route_through_decide() -> None:
    backed = sorted(n for n, b in DECISIONS.items() if b().approvals_backed)
    assert backed, "expected approvals-backed decisions (timesheet, expense, leave, extension, ...)"


@pytest.mark.parametrize("name", sorted(PENDING_HUMAN_ONLY) or ["-"])
def test_pending_human_only(name: str) -> None:
    if name == "-":
        pytest.skip("no pending decisions")
    assert name not in DECISIONS, f"{name} is mapped now: remove it from PENDING_HUMAN_ONLY"
    pytest.skip(f"FR-181 pending: {name} -- {PENDING_HUMAN_ONLY[name]}")


def test_pending_record_payment_still_unguarded() -> None:
    """Fails once ``payments.record`` gains a human-only guard: then map it and drop it from pending."""
    from app.services.receivables import payments

    if "record Payment" not in PENDING_HUMAN_ONLY:
        pytest.skip("record Payment no longer pending")
    src = inspect.getsource(payments.record)
    assert "_actor_check" not in src and "require_human" not in src, (
        "payments.record is guarded now: map it in DECISIONS and remove it from PENDING_HUMAN_ONLY"
    )
