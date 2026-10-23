# ruff: file-ignore[line-too-long]
"""Screening Agent, internal API (Epic 57 A7): questionnaire versions / bindings, conversations, review, settings.

Permissions: ``screening:conduct`` (questionnaires, start / pause / resume / take over / cancel, recruiter messages,
review), ``application:read`` (conversation reads), ``ai:agent_configure`` (settings). Starting, approving a version,
accepting AI questions, deciding answers and completing a review are human only (403 ``human_only_action``).
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.actors import actor_from_ctx, require_human
from app.core.context import get_context
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.screening_agent import (
    AnswerDecisionIn,
    AnswerOut,
    BindingIn,
    BindingOut,
    ConversationActionIn,
    ConversationDetailOut,
    ConversationOut,
    ConversationStartIn,
    GeneratedQuestionsOut,
    GenerateQuestionsIn,
    RecruiterMessageIn,
    ReviewCompleteIn,
    ReviewOut,
    ScreeningAgentSettingsIn,
    ScreeningAgentSettingsOut,
    StartOptionsOut,
    TurnOut,
    VersionCreate,
    VersionOut,
    VersionUpdate,
)
from app.services import screening_agent_service as svc

router = APIRouter(tags=["screening-agent"])
DB = TenantDBSessionDep
Conduct = Annotated[Principal, Depends(require("screening:conduct"))]
Read = Annotated[Principal, Depends(require("application:read"))]
Configure = Annotated[Principal, Depends(require("ai:agent_configure"))]
SettingsRead = Annotated[Principal, Depends(require_any("ai:agent_configure", "screening:conduct"))]


def _human() -> None:
    require_human(actor_from_ctx(get_context()))


# ================================================================ questionnaires
@router.get("/screening-questionnaires/{questionnaire_id}/versions", response_model=list[VersionOut])
async def list_versions(questionnaire_id: uuid.UUID, _: Conduct, db: DB) -> Any:
    return await svc.questionnaires.list_versions(get_context(), db, questionnaire_id)


@router.post(
    "/screening-questionnaires/{questionnaire_id}/versions",
    response_model=VersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_version(questionnaire_id: uuid.UUID, body: VersionCreate, _: Conduct, db: DB) -> Any:
    return await svc.questionnaires.create_version(get_context(), db, questionnaire_id, body)


@router.patch("/screening-questionnaire-versions/{version_id}", response_model=VersionOut)
async def update_version(version_id: uuid.UUID, body: VersionUpdate, _: Conduct, db: DB) -> Any:
    return await svc.questionnaires.update_version(get_context(), db, version_id, body)


@router.post("/screening-questionnaire-versions/{version_id}/approve", response_model=VersionOut)
async def approve_version(version_id: uuid.UUID, _: Conduct, db: DB) -> Any:
    _human()
    return await svc.questionnaires.approve_version(get_context(), db, version_id)


@router.post(
    "/screening-questionnaire-versions/{version_id}/questions/{question_id}/accept", response_model=VersionOut
)
async def accept_question(version_id: uuid.UUID, question_id: uuid.UUID, _: Conduct, db: DB) -> Any:
    _human()
    return await svc.questionnaires.accept_question(get_context(), db, version_id, question_id)


@router.delete(
    "/screening-questionnaire-versions/{version_id}/questions/{question_id}", response_model=VersionOut
)
async def remove_question(version_id: uuid.UUID, question_id: uuid.UUID, _: Conduct, db: DB) -> Any:
    return await svc.questionnaires.remove_question(get_context(), db, version_id, question_id)


@router.post(
    "/screening-questionnaires/{questionnaire_id}/generate-questions", response_model=GeneratedQuestionsOut
)
async def generate_questions(
    questionnaire_id: uuid.UUID, body: GenerateQuestionsIn, _: Conduct, db: DB
) -> Any:
    return await svc.questionnaires.generate_questions(get_context(), db, questionnaire_id, body.demand_id)


@router.get("/screening-questionnaires/{questionnaire_id}/bindings", response_model=list[BindingOut])
async def list_bindings(questionnaire_id: uuid.UUID, _: Conduct, db: DB) -> Any:
    return await svc.questionnaires.list_bindings(get_context(), db, questionnaire_id)


@router.put("/screening-questionnaires/{questionnaire_id}/bindings", response_model=BindingOut)
async def bind(questionnaire_id: uuid.UUID, body: BindingIn, _: Conduct, db: DB) -> Any:
    return await svc.questionnaires.bind(get_context(), db, questionnaire_id, body)


@router.delete(
    "/screening-questionnaires/{questionnaire_id}/bindings", status_code=status.HTTP_204_NO_CONTENT
)
async def unbind(
    questionnaire_id: uuid.UUID,
    _: Conduct,
    db: DB,
    scope_kind: Annotated[str, Query(pattern="^(tenant|client|demand)$")],
    scope_id: uuid.UUID | None = None,
) -> Response:
    await svc.questionnaires.unbind(get_context(), db, questionnaire_id, scope_kind, scope_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ================================================================ conversations
@router.get("/applications/{application_id}/screening-start-options", response_model=StartOptionsOut)
async def start_options(application_id: uuid.UUID, _: Conduct, db: DB) -> Any:
    return await svc.conversations.start_options(get_context(), db, application_id)


@router.post(
    "/applications/{application_id}/screening-conversations",
    response_model=ConversationOut,
    status_code=status.HTTP_201_CREATED,
)
async def start(
    application_id: uuid.UUID,
    body: ConversationStartIn,
    _: Conduct,
    db: DB,
    idem: Annotated[IdempotencyGuard, Depends(idempotency())],
) -> Any:
    _human()
    ctx = get_context()

    async def _run() -> ConversationOut:
        conv = await svc.conversations.start(
            ctx, db, application_id, channel=body.channel, trigger_kind="manual"
        )
        return await svc.conversations.conversation_out(db, conv)

    return await idem.run(_run)


@router.get("/applications/{application_id}/screening-conversations", response_model=list[ConversationOut])
async def list_conversations(application_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.conversations.list_for_application(get_context(), db, application_id)


@router.get("/screening-conversations/{conversation_id}", response_model=ConversationDetailOut)
async def get_conversation(
    conversation_id: uuid.UUID, _: Read, db: DB, after_seq: Annotated[int, Query(ge=0)] = 0
) -> Any:
    return await svc.conversations.get(get_context(), db, conversation_id, after_seq)


async def _action(conversation_id: uuid.UUID, db: Any, fn: Any) -> ConversationOut:
    ctx = get_context()
    await svc.conversations.readable(ctx, db, conversation_id)
    conv = await fn(ctx, db, conversation_id)
    return await svc.conversations.conversation_out(db, conv)


@router.post("/screening-conversations/{conversation_id}/pause", response_model=ConversationOut)
async def pause(conversation_id: uuid.UUID, body: ConversationActionIn, _: Conduct, db: DB) -> Any:
    return await _action(conversation_id, db, lambda c, d, i: svc.pause(c, d, i, reason=body.reason))


@router.post("/screening-conversations/{conversation_id}/resume", response_model=ConversationOut)
async def resume(conversation_id: uuid.UUID, _: Conduct, db: DB) -> Any:
    _human()
    return await _action(conversation_id, db, svc.conversations.resume)


@router.post("/screening-conversations/{conversation_id}/take-over", response_model=ConversationOut)
async def take_over(conversation_id: uuid.UUID, _: Conduct, db: DB) -> Any:
    _human()
    return await _action(conversation_id, db, svc.conversations.take_over)


@router.post("/screening-conversations/{conversation_id}/cancel", response_model=ConversationOut)
async def cancel(conversation_id: uuid.UUID, _: Conduct, db: DB) -> Any:
    return await _action(conversation_id, db, svc.conversations.cancel)


@router.post(
    "/screening-conversations/{conversation_id}/messages",
    response_model=TurnOut,
    status_code=status.HTTP_201_CREATED,
)
async def recruiter_message(
    conversation_id: uuid.UUID, body: RecruiterMessageIn, principal: Conduct, db: DB
) -> Any:
    _human()
    from app.core.permissions import check

    ctx = get_context()
    conv = await svc.conversations.readable(ctx, db, conversation_id)
    if conv.channel == "email":
        check(principal, "email:send")
    elif conv.channel == "whatsapp":
        check(principal, "messaging:send")
    turn = await svc.conversations.post_recruiter_message(ctx, db, conversation_id, body.body)
    from app.services.screening_agent import turns

    return turns.turn_out(turn)


# ================================================================ review
@router.get("/screening-reviews", response_model=ReviewOut)
async def get_review(
    _: Conduct, db: DB, conversation_id: uuid.UUID | None = None, voice_session_id: uuid.UUID | None = None
) -> Any:
    return await svc.review.get(
        get_context(), db, conversation_id=conversation_id, voice_session_id=voice_session_id
    )


@router.post("/screening-answers/{answer_id}/decision", response_model=AnswerOut)
async def decide(answer_id: uuid.UUID, body: AnswerDecisionIn, _: Conduct, db: DB) -> Any:
    _human()
    return await svc.decide_answer(get_context(), db, answer_id, body)


@router.post("/screening-reviews/complete", response_model=ReviewOut)
async def complete(body: ReviewCompleteIn, _: Conduct, db: DB) -> Any:
    _human()
    return await svc.complete_review(
        get_context(), db, conversation_id=body.conversation_id, voice_session_id=body.voice_session_id
    )


# ================================================================ settings
@router.get("/settings/agents/screening", response_model=ScreeningAgentSettingsOut)
async def get_settings(_: SettingsRead, db: DB) -> Any:
    return await svc.agent_settings.get(get_context(), db)


@router.put("/settings/agents/screening", response_model=ScreeningAgentSettingsOut)
async def put_settings(body: ScreeningAgentSettingsIn, _: Configure, db: DB) -> Any:
    _human()
    return await svc.agent_settings.update(get_context(), db, body)
