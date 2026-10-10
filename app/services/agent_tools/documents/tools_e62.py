# ruff: noqa: E501
"""Documents tool for the Onboarding Agent (Epic 62 A4): ``documents.list_my_signature_requests`` (read, thread-bound:
the asker's pending eSign request titles and expiry dates; no document content or links)."""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.services import knowledge_service


class ThreadIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: uuid.UUID


class SignatureView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    status: str
    expires_on: str


class SignaturesOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests: list[SignatureView]


async def _list(tctx: Any, args: ThreadIn) -> ToolOutcome:
    from app.models.documents import OPEN_REQUEST_STATUSES, SignatureRequest, Signer

    thread = await knowledge_service.qa.load_for_tool(tctx.db, args.thread_id, tctx.subject)
    rows = (
        await tctx.db.execute(
            select(SignatureRequest.title, SignatureRequest.status, SignatureRequest.expires_at)
            .join(Signer, Signer.request_id == SignatureRequest.id)
            .where(
                Signer.kind == "candidate", Signer.principal_id == thread.candidate_id,
                Signer.status == "pending", SignatureRequest.status.in_(OPEN_REQUEST_STATUSES),
            )
            .order_by(SignatureRequest.expires_at)
            .limit(20)
        )
    ).all()  # fmt: skip
    data = SignaturesOut(
        requests=[SignatureView(title=t[:300], status=s, expires_on=e.date().isoformat()) for t, s, e in rows]
    )
    return ToolOutcome(
        data, refs=(("onboarding_qa_thread", thread.id),), candidate_ids=(thread.candidate_id,),
        target=("onboarding_qa_thread", thread.id), summary=f"{len(data.requests)} signature requests",
    )  # fmt: skip


SPEC = ToolSpec(
    key="documents.list_my_signature_requests", version=1, owner_epic="62", title="List the asker's documents to sign",
    description="List the asker's pending e-signature requests (title, status, expiry date). Titles only; no document content or links. Read-only.",
    input_model=ThreadIn, output_model=SignaturesOut, permissions=("document:read",), risk="read",
    scope=ScopeRule(resource="onboarding_qa_thread", target_arg="thread_id"), handler=_list,
)  # fmt: skip


def register() -> None:
    from app.core.agentic.tools import tools

    if SPEC.key not in tools():
        register_tool(SPEC)


register()
