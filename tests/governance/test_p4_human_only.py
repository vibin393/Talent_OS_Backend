# ruff: noqa: E501
"""FR-325 governance suite (Epic 65 story 65.7, spec A7): Phase 4 human-only (★) decisions.

Same structure as ``test_p3_human_only.py``: probes drive the owner's guard with an automated actor and never touch
the database (``_FakeDB``: ``get`` returns a stub, any SQL raises). Accepted refusals: 403 ``human_only_action`` or
409 ``transition_not_allowed``.

Checks: (a) every row is registered in ``workflow.catalogue.human_only_transitions()`` or guarded by
``require_human``; (b) completeness -- every human-only target registered by a Phase 4 owner is mapped here
("add the ★ row"); (c) probes per row x automated kind; (d) the tool reachability test runs over every registered
tool and ``PENDING_P4_STAR`` is empty; (e) ``FORBIDDEN_ACTIONS`` covers the P1-P3 ★ catalogue plus the FR-325 list
and no tool's transition is in it.

Rows: screening outcome (text + voice), scorecard submit (``require_human`` in ``interviews.scorecards.submit_mine``;
P4 code also may not import ``interviews.scorecards``), skill evidence -> verified (interview evidence,
Epic 60 / C-10, enforced by ``require_human``) and P3 ``candidate_skill -> verified``, Proposal decisions, Copilot
draft confirmation, autonomy settings and the kill switch.
"""

import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.context import RequestContext, reset_context, set_context
from app.core.errors import DomainError

AUTOMATED = ("agent", "ai", "workflow", "system", "api_key", "app")  # Epic 66: +app
ACCEPTED = {("human_only_action", 403), ("transition_not_allowed", 409)}
PENDING_HUMAN_ONLY_P4: dict[str, str] = {}  # shrink-only; must be empty at phase close

# Entity types whose ★ targets are registered by Phase 4 owners.
P4_ENTITY_TYPES = frozenset({"screening_conversation", "screening_answer", "voice_session", "skill_evidence"})


def _ctx(kind: str) -> RequestContext:
    if kind == "app":  # Epic 66: an App Principal (delegated token)
        from app.core.permissions import Principal

        inst = uuid.uuid4()
        return RequestContext(
            tenant_id=uuid.uuid4(),
            principal=Principal(type="app", id=inst, tenant_id=uuid.uuid4(), app_installation_id=inst, acting_user_id=uuid.uuid4()),
        )
    if kind == "api_key":
        from app.core.permissions import Principal

        return RequestContext(
            tenant_id=uuid.uuid4(),
            principal=Principal(type="api_key", id=uuid.uuid4(), tenant_id=uuid.uuid4()),
        )
    return RequestContext(tenant_id=uuid.uuid4(), actor_kind=kind)


class _FakeDB:
    def __init__(self, row: Any = None) -> None:
        self.row = row

    async def get(self, *_a: Any, **_k: Any) -> Any:
        return self.row

    async def execute(self, *_a: Any, **_k: Any) -> Any:
        raise AssertionError("probe reached the database: the human-only guard must run first")

    async def flush(self) -> None:
        raise AssertionError("probe reached the database: the human-only guard must run first")


@dataclasses.dataclass(frozen=True)
class Row:
    name: str
    owner: str
    probe: Callable[[RequestContext], Awaitable[object]] | None  # None = static guard (import contract)
    registered: tuple[str, tuple[str, ...]] | None = None  # (entity_type, targets) in the workflow catalogue


async def _screening_reviewed(ctx: RequestContext) -> object:
    from app.services.screening_agent import review

    return await review.complete(ctx, _FakeDB(), conversation_id=uuid.uuid4())  # type: ignore[arg-type]


async def _screening_answer(ctx: RequestContext) -> object:
    from app.schemas.screening_agent import AnswerDecisionIn
    from app.services.screening_agent import review

    return await review.decide_answer(
        ctx, _FakeDB(), uuid.uuid4(), AnswerDecisionIn.model_construct(decision="accepted")
    )  # type: ignore[arg-type]


async def _voice_reviewed(ctx: RequestContext) -> object:
    from app.services.voice import sessions

    return await sessions.complete_review(ctx, _FakeDB(), uuid.uuid4())  # type: ignore[arg-type]


async def _skill_evidence(ctx: RequestContext) -> object:
    from app.services.skills import inference

    return await inference._evidence_kind(ctx, _FakeDB(), SimpleNamespace(source="interview"))  # type: ignore[arg-type]


async def _candidate_skill(ctx: RequestContext) -> object:
    from app.schemas.skills import EvidenceIn
    from app.services.skills import evidence

    stub = SimpleNamespace(id=uuid.uuid4(), candidate_id=uuid.uuid4())
    return await evidence.add_evidence(ctx, _FakeDB(stub), uuid.uuid4(), EvidenceIn.model_construct(kind="recruiter"))  # type: ignore[arg-type]


async def _scorecard_submit(ctx: RequestContext) -> object:
    from app.services.interviews import scorecards

    return await scorecards.submit_mine(ctx, _FakeDB(), uuid.uuid4(), None)  # type: ignore[arg-type]


async def _decide(ctx: RequestContext) -> object:
    from app.schemas.agentic import ProposalDecisionIn
    from app.services import agent_runtime_service as svc

    return await svc.decide_proposal(ctx, _FakeDB(), uuid.uuid4(), ProposalDecisionIn(decision="approve"))  # type: ignore[arg-type]


async def _bulk(ctx: RequestContext) -> object:
    from app.schemas.agentic import BulkDecisionIn
    from app.services import agent_runtime_service as svc

    return await svc.bulk_decide(
        ctx, _FakeDB(), BulkDecisionIn(proposal_ids=[uuid.uuid4()], decision="approve")
    )  # type: ignore[arg-type]


async def _confirm(ctx: RequestContext) -> object:
    from app.services import agent_runtime_service as svc

    return await svc.confirm_draft(ctx, _FakeDB(), uuid.uuid4())  # type: ignore[arg-type]


async def _discard(ctx: RequestContext) -> object:
    from app.services import agent_runtime_service as svc

    return await svc.discard_draft(ctx, _FakeDB(), uuid.uuid4())  # type: ignore[arg-type]


def _route(call: Callable[[], Awaitable[object]]) -> Callable[[RequestContext], Awaitable[object]]:
    """Router-level guard (``_human()`` reads the request context before any service call)."""

    async def probe(ctx: RequestContext) -> object:
        token = set_context(ctx)
        try:
            return await call()
        finally:
            reset_context(token)

    return probe


def _agents_route(name: str, *args: Any) -> Callable[[RequestContext], Awaitable[object]]:
    async def call() -> object:
        from app.router import agents

        return await getattr(agents, name)(*args, None, _FakeDB())

    return _route(call)


def _gov_route(name: str, *args: Any) -> Callable[[RequestContext], Awaitable[object]]:
    async def call() -> object:
        from app.router import ai_governance

        return await getattr(ai_governance, name)(*args, None, _FakeDB())

    return _route(call)


async def _burn_in_service(ctx: RequestContext) -> object:
    from app.services.agent_runtime.governance import burn_in

    return await burn_in.end_early(ctx, _FakeDB(), "screening", "a long enough reason")  # type: ignore[arg-type]


def _rows() -> list[Row]:
    from app.schemas.agentic import ActionPolicyIn, AgentConfigIn, AgentEnabledIn, EnablementIn, KillSwitchIn
    from app.schemas.ai_governance import ReasonIn

    reason = ReasonIn(reason="a long enough reason")
    return [
        Row(
            "screening_outcome_text",
            "Epic 57",
            _screening_reviewed,
            ("screening_conversation", ("reviewed",)),
        ),
        Row(
            "screening_answer_decision",
            "Epic 57",
            _screening_answer,
            ("screening_answer", ("accepted", "edited", "rejected")),
        ),
        Row("screening_outcome_voice", "Epic 58", _voice_reviewed, ("voice_session", ("reviewed",))),
        Row("scorecard_submit", "P1", _scorecard_submit),
        Row("skill_evidence_verified", "Epic 60", _skill_evidence),
        Row("candidate_skill_verified", "Epic 37", _candidate_skill, ("candidate_skill", ("verified",))),
        Row("proposal_decide", "Epic 53", _decide),
        Row("proposal_bulk_decide", "Epic 53", _bulk),
        Row("copilot_draft_confirm", "Epic 53", _confirm),
        Row("copilot_draft_discard", "Epic 53", _discard),
        Row(
            "autonomy_set_action_policy",
            "Epic 53",
            _agents_route("set_action", "screening", "messaging.send", ActionPolicyIn(mode="suggest")),
        ),
        Row("autonomy_update_config", "Epic 53", _agents_route("update_agent", "screening", AgentConfigIn())),
        Row(
            "autonomy_set_enabled",
            "Epic 53",
            _agents_route("set_enabled", "screening", AgentEnabledIn(enabled=True)),
        ),
        Row(
            "autonomy_acknowledge_enablement",
            "Epic 53",
            _agents_route("enable_agentic", EnablementIn(notice_version="x")),
        ),
        Row("autonomy_burn_in_end", "Epic 65", _burn_in_service),
        Row("autonomy_burn_in_end_route", "Epic 65", _gov_route("end_burn_in", "screening", reason)),
        Row(
            "autonomy_checklist_acknowledge",
            "Epic 65",
            _gov_route("acknowledge_checklist", "screening", reason),
        ),
        Row("kill_switch", "Epic 53", _agents_route("kill_switch", KillSwitchIn(on=True, reason="drill"))),
    ]


ROWS = {r.name: r for r in _rows()}


def _catalogue() -> dict[str, frozenset[str]]:
    from app.core.hooks import autodiscover
    from app.services.workflow.catalogue import human_only_transitions

    autodiscover("app.services")
    return dict(human_only_transitions())


@pytest.mark.parametrize("name", sorted(n for n, r in ROWS.items() if r.registered))
def test_registered_rows_are_in_catalogue(name: str) -> None:
    entity, targets = ROWS[name].registered  # type: ignore[misc]
    assert set(targets) <= _catalogue().get(entity, frozenset()), f"{name}: {entity} {targets} not registered"


def test_completeness_every_p4_star_target_is_mapped() -> None:
    mapped: dict[str, set[str]] = {}
    for r in ROWS.values():
        if r.registered:
            mapped.setdefault(r.registered[0], set()).update(r.registered[1])
    unmapped = {
        e: sorted(set(t) - mapped.get(e, set())) for e, t in _catalogue().items()
        if e in P4_ENTITY_TYPES and set(t) - mapped.get(e, set())
    }  # fmt: skip
    assert unmapped == {}, f"add the ★ row for: {unmapped}"


@pytest.mark.parametrize("kind", AUTOMATED)
@pytest.mark.parametrize("name", sorted(n for n, r in ROWS.items() if r.probe is not None))
async def test_row_refuses_automated_actor(name: str, kind: str) -> None:
    probe = ROWS[name].probe
    assert probe is not None
    with pytest.raises(DomainError) as info:
        await probe(_ctx(kind))
    assert (info.value.code, info.value.status_code) in ACCEPTED, f"{name}/{kind}: {info.value.code}"


def test_scorecard_submit_is_out_of_reach_of_phase4_code() -> None:
    """Defence in depth on top of the ``require_human`` guard: P4 code may never import ``interviews.scorecards``
    (``p4-human-only`` forbidden list)."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / ".importlinter").read_text(encoding="utf-8")
    section = text.split("[importlinter:contract:p4-human-only]", 1)[1].split("[importlinter:contract:", 1)[0]
    assert "app.services.interviews.scorecards" in section


def test_pending_lists_are_empty() -> None:
    from tests.governance.test_p4_tool_star_reachability import PENDING_P4_STAR

    assert PENDING_P4_STAR == {}
    assert PENDING_HUMAN_ONLY_P4 == {}


def test_forbidden_actions_cover_the_star_catalogue() -> None:
    from app.core.agentic import FORBIDDEN_ACTIONS, tools

    _catalogue()
    forbidden = set(FORBIDDEN_ACTIONS)
    fr325 = {
        ("screening", "outcome"),
        ("scorecard", "submitted"),
        ("offer", "released"),
        ("application", "rejected"),
    }
    assert fr325 <= forbidden
    performed = {spec.transition for spec in tools().values() if spec.transition is not None}
    assert not performed & forbidden
