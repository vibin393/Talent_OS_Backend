# ruff: noqa: E501
"""Epic 57 tool ``candidates.get_contact_channels`` (read): which channels can reach the conversation's Candidate.
No address or number is ever returned."""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.services import screening_agent_service as svc


class ChannelsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID


class WhatsAppState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opted_in: bool
    window_open: bool


class ChannelsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portal: bool
    email: bool
    whatsapp: WhatsAppState
    sms: bool


async def _handler(tctx: Any, args: ChannelsIn) -> ToolOutcome:
    conv = await svc.ops.load(tctx.db, args.conversation_id, tctx.subject)
    data = await svc.ops.contact_channels(tctx.db, conv)
    return ToolOutcome(
        ChannelsOut.model_validate(data), candidate_ids=(conv.candidate_id,),
        target=("screening_conversation", conv.id), summary="contact channels read",
    )  # fmt: skip


register_tool(
    ToolSpec(
        key="candidates.get_contact_channels",
        version=1,
        owner_epic="57",
        title="Check how to reach the candidate",
        description="Whether the conversation's candidate can be reached on the portal, email, WhatsApp (opt-in, 24-hour window) or SMS. Never returns addresses.",
        input_model=ChannelsIn,
        output_model=ChannelsOut,
        permissions=("candidate:read",),
        risk="read",
        scope=ScopeRule(resource="screening_conversation", target_arg="conversation_id", cross_subject=True),
        handler=_handler,
    )
)
