# ruff: noqa: E501
"""Epic 57 tool ``postings.get`` (read): the published posting fields of a screening conversation's Demand, the only
source of Grounded Answers. Field values are third-party text (``untrusted_output``). Pay / rates are never returned."""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.services import screening_agent_service as svc


class PostingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID


class PostingField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    value: str


class PostingOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: list[PostingField]


async def _handler(tctx: Any, args: PostingIn) -> ToolOutcome:
    conv = await svc.ops.load(tctx.db, args.conversation_id, tctx.subject)
    fields = await svc.ops.postings(tctx.db, conv)
    return ToolOutcome(
        PostingOut(fields=[PostingField(**f) for f in fields]), refs=(("demand", conv.demand_id),),
        target=("screening_conversation", conv.id), summary=f"{len(fields)} posting fields",
    )  # fmt: skip


register_tool(
    ToolSpec(
        key="postings.get",
        version=1,
        owner_epic="57",
        title="Read the published job posting",
        description="Published posting fields (key, label, value) of the conversation's job. Empty when the job is not published. Use only these to answer job questions.",
        input_model=PostingIn,
        output_model=PostingOut,
        permissions=("demand:read",),
        risk="read",
        scope=ScopeRule(resource="screening_conversation", target_arg="conversation_id", cross_subject=True),
        handler=_handler,
        untrusted_output=frozenset({"fields.value"}),
    )
)
