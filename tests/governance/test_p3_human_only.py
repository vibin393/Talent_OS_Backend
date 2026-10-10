# ruff: noqa: E501
"""FR-257 governance suite (Epic 52 story 52.2, spec A2): Phase 3 human-only (★) decisions.

Same structure as ``test_human_only_p2.py``: ``DECISIONS`` maps each ★ decision to ``Decision(module, table,
rows, declared, probe)``; probes drive the owner's guard with an automated actor (``ai`` / ``workflow`` /
``system``) and never touch the database (``db=None`` or a ``_FakeDB`` stand-in whose loaders return stubs; any
real query raises). Accepted refusals: 403 ``human_only_action`` or 409 ``transition_not_allowed``.

Checks: (a) every ★ row is registered in ``workflow.catalogue.human_only_transitions()``; (b) completeness --
every target registered for a Phase 3 entity type (anything outside the Phase 2 baseline) is mapped here;
(c) probes per row x automated kind; (d) ``register_action`` refuses a workflow action that performs a ★ row.

Adaptation (recorded in the Epic 52 notes): Epic 37 registers ``candidate_skill -> verified`` (recruiter
verification evidence), so it has a ★ row here although the spec table does not list it.
"""

import dataclasses
import inspect
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.actors import HUMAN_KINDS, ActorRef
from app.core.context import RequestContext
from app.core.errors import DomainError

AUTOMATED = ("ai", "workflow", "system", "agent", "app")  # Epic 53: +agent; Epic 66: +app
ACCEPTED = {("human_only_action", 403), ("transition_not_allowed", 409)}

# Entity types with human-only targets registered by Phase 1 / Phase 2 (covered by test_human_only_p2.py).
P2_BASELINE_ENTITY_TYPES = frozenset(
    {
        "accounting_period", "application", "approval_request", "bgv_case", "demand", "invoice",
        "onboarding_task", "payment", "payroll_export", "vendor_submission",
    }
)  # fmt: skip


def _ctx(kind: str) -> RequestContext:
    return RequestContext(tenant_id=uuid.uuid4(), actor_kind=kind)


def _actor(kind: str) -> ActorRef:
    return ActorRef(kind, None)  # type: ignore[arg-type]


class _FakeDB:
    """A session stand-in: ``get`` returns a stub row; any SQL raises (probes must refuse before querying)."""

    def __init__(self, row: Any) -> None:
        self.row = row

    async def get(self, *_a: Any, **_k: Any) -> Any:
        return self.row

    async def execute(self, *_a: Any, **_k: Any) -> Any:
        raise AssertionError("probe reached the database: the human-only guard must run first")

    async def flush(self) -> None:
        raise AssertionError("probe reached the database: the human-only guard must run first")


@dataclasses.dataclass(frozen=True)
class Decision:
    entity_type: str
    targets: tuple[str, ...]
    owner: str
    module: str
    table: str
    rows: tuple[tuple[str, str], ...]
    declared: Callable[[], bool]
    probe: Callable[[str], Awaitable[object] | object]


def _source_has(fn: Callable[..., Any], marker: str) -> bool:
    return marker in inspect.getsource(fn)


async def _decide(kind: str) -> None:
    from app.services.approvals import service

    await service.decide(_ctx(kind), None, uuid.uuid4(), None, actor=_actor(kind))  # type: ignore[arg-type]


# ------------------------------------------------------------------ ★ rows
def _assessment() -> Decision:
    from app.services.assessments import lifecycle as m

    rows = (("reviewed", "passed"), ("reviewed", "failed"))

    def declared() -> bool:
        # Only human deciders and the conditional non-gating system auto-pass (spec section 5.1).
        return (
            all(m.TRANSITIONS[r] - {"system"} <= HUMAN_KINDS for r in rows)
            and "system" not in m.TRANSITIONS[rows[1]]
        )

    def probe(kind: str) -> None:
        for frm, to in rows:
            m.require(
                frm, to, kind, gating=True, auto_pass_allowed=True, score=Decimal(100), threshold=Decimal(1)
            )

    return Decision(
        "assessment_invitation",
        ("passed", "failed"),
        "Epic 40",
        m.__name__,
        "TRANSITIONS",
        rows,
        declared,
        probe,
    )


def _campus_override() -> Decision:
    from app.services.campus import eligibility as m

    async def probe(kind: str) -> None:
        await m.override(_ctx(kind), None, uuid.uuid4(), "manual review")  # type: ignore[arg-type]

    return Decision(
        "campus_registration", ("eligibility_override",), "Epic 43", m.__name__, "(guard in override)",
        (("not_eligible", "eligible"),), lambda: _source_has(m.override, "require_human(actor)"), probe,
    )  # fmt: skip


def _campus_shortlist() -> Decision:
    from app.services.campus import registration as reg
    from app.services.campus import shortlist as m

    rows = (("assessed", "shortlisted"),)

    async def probe(kind: str) -> None:
        for frm, to in rows:
            with pytest.raises(DomainError) as info:
                reg.set_status(SimpleNamespace(status=frm), to, kind)  # type: ignore[arg-type]
            assert (info.value.code, info.value.status_code) in ACCEPTED
        data = SimpleNamespace(registration_ids=[uuid.uuid4()], demand_id=uuid.uuid4())
        await m.confirm(_ctx(kind), None, uuid.uuid4(), data)  # type: ignore[arg-type]

    def declared() -> bool:
        return all(reg.TRANSITIONS[r] <= HUMAN_KINDS for r in rows) and _source_has(
            m.confirm, "require_human(actor)"
        )

    return Decision(
        "campus_registration",
        ("shortlisted",),
        "Epic 43",
        m.__name__,
        "registration.TRANSITIONS",
        rows,
        declared,
        probe,
    )


def _mobility_transfer() -> Decision:
    from app.services.mobility import transfers as m

    async def probe(kind: str) -> None:
        await m.confirm(_ctx(kind), None, uuid.uuid4(), SimpleNamespace())  # type: ignore[arg-type]

    return Decision(
        "internal_application", ("transfer_confirmed",), "Epic 41", m.__name__, "(guard in confirm)",
        (("selected", "transfer_confirmed"),), lambda: _source_has(m.confirm, "require_human(actor)"), probe,
    )  # fmt: skip


def _referral_reward() -> Decision:
    from app.services.referrals import rewards as m

    async def probe(kind: str) -> None:
        await m.mark_approved(_FakeDB(None), SimpleNamespace(status="eligible"), _actor(kind))  # type: ignore[arg-type]
        await _decide(kind)  # the approval engine refuses non-approver kinds too

    return Decision(
        "referral_reward", ("approved",), "Epic 42", m.__name__, "(guard in mark_approved)",
        (("eligible", "approved"),), lambda: _source_has(m.mark_approved, "require_human(actor)"), probe,
    )  # fmt: skip


def _workforce_plan() -> Decision:
    from app.services.workforce import approval as m

    return Decision(
        "workforce_plan", ("approved",), "Epic 44", m.__name__, "(approvals subject; guard in on_final_decided)",
        (("pending_approval", "approved"),), lambda: _source_has(m.on_final_decided, "require_human(actor)"), _decide,
    )  # fmt: skip


def _deliverable() -> Decision:
    from app.services.projects import deliverables as m

    async def probe(kind: str) -> None:
        row = SimpleNamespace(status="submitted")
        await m.accept(_ctx(kind), _FakeDB(row), row, _actor(kind))  # type: ignore[arg-type]

    def declared() -> bool:
        return frozenset({"client_user", "recorded_by_user"}) == m.ACCEPTOR_KINDS and _source_has(
            m.accept, "require_human(actor)"
        )

    return Decision(
        "deliverable",
        ("accepted",),
        "Epic 45",
        m.__name__,
        "(guard in accept)",
        (("submitted", "accepted"),),
        declared,
        probe,
    )


def _immigration() -> Decision:
    from app.services.immigration import cases as m
    from app.services.immigration import transitions as t

    async def probe(kind: str) -> None:
        mp = pytest.MonkeyPatch()
        mp.setattr(m, "require", lambda *_a, **_k: None)  # permission check is not under test
        try:
            for outcome in ("approved", "refused"):
                await m.record_outcome(_ctx(kind), None, uuid.uuid4(), SimpleNamespace(outcome=outcome))  # type: ignore[arg-type]
        finally:
            mp.undo()

    return Decision(
        "immigration_case", ("approved", "refused"), "Epic 46", m.__name__, "transitions.HUMAN_ONLY",
        (("in_stage", "approved"), ("in_stage", "refused")),
        lambda: set(t.HUMAN_ONLY) == {"approved", "refused"} and _source_has(m.record_outcome, "require_human("),
        probe,
    )  # fmt: skip


def _tds_match() -> Decision:
    from app.services.receivables.tds_recon import runs as m

    async def probe(kind: str) -> None:
        await m.confirm(_ctx(kind), None, uuid.uuid4())  # type: ignore[arg-type]

    return Decision(
        "tds_recon_match", ("confirmed",), "Epic 51", m.__name__, "(guard in confirm)",
        (("proposed", "confirmed"),), lambda: _source_has(m.confirm, "require_human(actor_from_ctx(ctx))"), probe,
    )  # fmt: skip


def _skill_verification() -> Decision:
    from app.services.skills import evidence as m

    async def probe(kind: str) -> None:
        cid = uuid.uuid4()
        db = _FakeDB(SimpleNamespace(id=uuid.uuid4(), candidate_id=cid))
        await m.add_evidence(
            _ctx(kind), db, uuid.uuid4(), SimpleNamespace(kind="recruiter"), candidate_id=cid
        )  # type: ignore[arg-type]

    return Decision(
        "candidate_skill", ("verified",), "Epic 37", m.__name__, "(guard in add_evidence, kind=recruiter)",
        (("declared", "verified"), ("inferred", "verified")),
        lambda: _source_has(m.add_evidence, "require_human(actor)"), probe,
    )  # fmt: skip


# Epic 57 (Phase 4): the Screening Agent review -- deciding extracted answers and completing the review are human only
# (the P1 screening outcome stays the P1 ★ path).
def _screening_review() -> Decision:
    from app.services.screening_agent import review as m

    async def probe(kind: str) -> None:
        await m.complete(_ctx(kind), None, conversation_id=uuid.uuid4())  # type: ignore[arg-type]

    return Decision(
        "screening_conversation", ("reviewed",), "Epic 57", m.__name__, "(guard in review.complete)",
        (("completed", "reviewed"),), lambda: _source_has(m.complete, "require_human(actor_from_ctx(ctx))"), probe,
    )  # fmt: skip


def _screening_answer() -> Decision:
    from app.schemas.screening_agent import AnswerDecisionIn
    from app.services.screening_agent import review as m

    async def probe(kind: str) -> None:
        await m.decide_answer(_ctx(kind), None, uuid.uuid4(), AnswerDecisionIn(decision="accept"))  # type: ignore[arg-type]

    return Decision(
        "screening_answer", ("accepted", "edited", "rejected"), "Epic 57", m.__name__, "(guard in review.decide_answer)",
        (("unconfirmed", "accepted"), ("unconfirmed", "edited"), ("unconfirmed", "rejected")),
        lambda: _source_has(m.decide_answer, "require_human(actor_from_ctx(ctx))"), probe,
    )  # fmt: skip


# Epic 58 (Phase 4): completing a voice screen review is human only (the P1 screening outcome stays the P1 ★ path).
def _voice_review() -> Decision:
    from app.services.voice import sessions as m

    async def probe(kind: str) -> None:
        await m.complete_review(_ctx(kind), None, uuid.uuid4())  # type: ignore[arg-type]

    return Decision(
        "voice_session", ("reviewed",), "Epic 58", m.__name__, "(guard in sessions.complete_review)",
        (("ready_for_review", "reviewed"),), lambda: _source_has(m.complete_review, "require_human(actor_from_ctx(ctx))"), probe,
    )  # fmt: skip


# Epic 69 (Phase 5): consenting to an Installation (test-gated activation, upgrade / re-consent, resume) is human only.
def _installation_grant() -> Decision:
    from app.services.marketplace import installs as m

    async def probe(kind: str) -> None:
        await m.upgrade(_ctx(kind), None, uuid.uuid4(), accept_manifest_sha256="0" * 64)  # type: ignore[arg-type]

    return Decision(
        "app_installation", ("active_from_pending", "upgrade", "resume"), "Epic 69", m.__name__, "(guards in installs.install / upgrade / test / set_status)",
        (("pending_consent", "active"), ("reconsent_required", "active"), ("suspended", "active")),
        lambda: all(_source_has(f, "require_human(actor_from_ctx(ctx))") for f in (m._require_grant, m.test, m.set_status)), probe,
    )  # fmt: skip


# Epic 71 (Phase 5): recording a ledger payment suggestion as a payment is human only (a Finance User confirms).
def _payment_suggestion() -> Decision:
    from app.services.ledger_sync import payments as m

    async def probe(kind: str) -> None:
        await m.confirm(_ctx(kind), None, uuid.uuid4())  # type: ignore[arg-type]

    return Decision(
        "payment_suggestion", ("confirmed",), "Epic 71", m.__name__, "(guard in confirm)",
        (("pending", "confirmed"),), lambda: _source_has(m.confirm, "require_human(actor_from_ctx(ctx))"), probe,
    )  # fmt: skip


DECISIONS: dict[str, Callable[[], Decision]] = {
    "Assessment Pass/Fail when gating": _assessment,
    "Campus eligibility override": _campus_override,
    "Campus shortlist confirmation": _campus_shortlist,
    "Internal transfer confirmation": _mobility_transfer,
    "Referral reward approval": _referral_reward,
    "Workforce Plan approval": _workforce_plan,
    "Deliverable acceptance": _deliverable,
    "Immigration outcome": _immigration,
    "26AS match confirmation": _tds_match,
    "Skill verification (recruiter evidence)": _skill_verification,
    "Screening review completion (Epic 57)": _screening_review,
    "Screening answer decision (Epic 57)": _screening_answer,
    "Voice screen review completion (Epic 58)": _voice_review,
    "Installation grant (Epic 69)": _installation_grant,
    "Payment suggestion confirmation (Epic 71)": _payment_suggestion,
}

# ★ decisions not enforced by any guard yet (decision -> owner / reason). Shrink-only; empty at phase close.
PENDING_HUMAN_ONLY: dict[str, str] = {}


def _catalogue() -> dict[str, frozenset[str]]:
    from app.core.hooks import autodiscover
    from app.services.workflow.catalogue import human_only_transitions

    autodiscover("app.services")
    return dict(human_only_transitions())


# ------------------------------------------------------------------ tests
@pytest.mark.parametrize("name", sorted(DECISIONS))
def test_row_is_registered_in_catalogue(name: str) -> None:
    d = DECISIONS[name]()
    registered = _catalogue().get(d.entity_type, frozenset())
    missing = sorted(set(d.targets) - registered)
    assert missing == [], f"{name} ({d.owner}): {d.entity_type} -> {missing} not registered human-only"


@pytest.mark.parametrize("name", sorted(DECISIONS))
def test_row_is_declared_human_only(name: str) -> None:
    d = DECISIONS[name]()
    assert d.rows, f"{name}: no transition rows mapped"
    assert d.declared(), f"{name}: {d.module}.{d.table} {d.rows} is not declared human-only"


def test_completeness_every_p3_star_target_is_mapped() -> None:
    mapped: dict[str, set[str]] = {}
    for build in DECISIONS.values():
        d = build()
        mapped.setdefault(d.entity_type, set()).update(d.targets)
    unmapped = {
        entity: sorted(targets - mapped.get(entity, set()))
        for entity, targets in _catalogue().items()
        if entity not in P2_BASELINE_ENTITY_TYPES
    }
    unmapped = {k: v for k, v in unmapped.items() if v}
    assert unmapped == {}, f"add the ★ row to DECISIONS for: {unmapped}"


@pytest.mark.parametrize("kind", AUTOMATED)
@pytest.mark.parametrize("name", sorted(DECISIONS))
async def test_row_refuses_automated_actor(name: str, kind: str) -> None:
    d = DECISIONS[name]()
    with pytest.raises(DomainError) as info:
        result = d.probe(kind)
        if inspect.isawaitable(result):
            await result
    err = info.value
    assert (err.code, err.status_code) in ACCEPTED, f"{name} ({kind}): got {err.status_code} {err.code}"


def test_non_gating_system_auto_pass_is_allowed() -> None:
    """Spec section 5.1: a non-gating invitation may be auto-passed by ``system`` at / above the threshold."""
    from app.services.assessments import lifecycle as m

    assert m.allowed(
        "reviewed",
        "passed",
        "system",
        gating=False,
        auto_pass_allowed=True,
        score=Decimal(80),
        threshold=Decimal(70),
    )
    assert not m.allowed(
        "reviewed",
        "passed",
        "system",
        gating=True,
        auto_pass_allowed=True,
        score=Decimal(80),
        threshold=Decimal(70),
    )
    assert not m.allowed("reviewed", "failed", "system", gating=False)


@pytest.mark.parametrize("kind", ("client_user", "recorded_by_user"))
def test_deliverable_acceptors_are_human(kind: str) -> None:
    from app.services.projects import deliverables as m

    assert kind in m.ACCEPTOR_KINDS and kind in HUMAN_KINDS


@pytest.mark.parametrize("name", sorted(DECISIONS))
def test_register_action_refuses_star_rows(name: str) -> None:
    from app.services.workflow.actions import ActionDef, AddTagParams, register_action

    async def _noop(*_a: Any) -> str:
        return ""

    d = DECISIONS[name]()
    _catalogue()
    for target in d.targets:
        definition = ActionDef(
            "probe",
            AddTagParams,
            lambda *_a: None,
            _noop,
            _noop,
            performs_transitions=((d.entity_type, target),),
        )  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="human-only"):
            register_action(f"e52.probe_{d.entity_type}_{target}", definition)


def test_phase_close_pending_human_only_is_empty() -> None:
    assert PENDING_HUMAN_ONLY == {}
