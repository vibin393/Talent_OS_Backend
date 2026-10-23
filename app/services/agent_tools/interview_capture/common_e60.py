# ruff: file-ignore[line-too-long]
"""Shared helpers of the Epic 60 Interview Assistant tools (not a tool module: the loader imports only ``tools_*.py``).

Every handler resolves the capture through ``interview_captures_service.agent_capture`` with the run's subject: a
capture of another Interview is ``not_found`` (the tools are ``cross_subject`` only so that the handler, not the
runtime's related-refs check, decides). Reads run under ``scope_context`` (the Accountable Owner's Effective Rights).
"""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.agentic import ProposalPreview
from app.services import agent_runtime_service as runtime

OWNER_EPIC = "60"
VERSION = 1


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


def scoped(tctx: Any) -> Any:
    return runtime.scope_context(tctx.request, tctx.owner_user_id)


def subject(tctx: Any) -> tuple[str, uuid.UUID] | None:
    return tctx.subject  # type: ignore[no-any-return]


def preview(title: str, describe: Any) -> Any:
    async def _preview(tctx: Any, args: Any) -> ProposalPreview:  # ruff: ignore[unused-function-argument]
        return ProposalPreview(title=title[:200], summary=str(describe(args))[:4000], renderer=None)

    return _preview


__all__ = ["OWNER_EPIC", "VERSION", "In", "Out", "preview", "scoped", "subject"]
