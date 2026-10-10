# ruff: noqa: E501
"""Epic 54 Copilot read tool ``predictions.get`` (FR-277): the Epic 48 predictions of one demand, offer or placement
you can see (``analytics.reads.predictions_for``) -- a supporting signal only. It never retrains or decides."""

import uuid
from typing import Any, Literal

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.services.agent_tools.copilot.common_e54 import OWNER_EPIC, In, Out, scoped


class PredictionIn(In):
    subject_kind: Literal["demand", "offer", "placement"]
    subject_id: uuid.UUID


class PredictionItem(Out):
    kind: str
    status: str
    band: str | None
    score_text: str | None
    model_version: int | None


class PredictionOut(Out):
    entries: list[PredictionItem]


async def _get(tctx: Any, args: PredictionIn) -> ToolOutcome:
    from app.services.analytics import reads

    out = await reads.predictions_for(scoped(tctx), tctx.db, args.subject_kind, args.subject_id)
    entries = []
    for p in out.predictions:
        text = p.score if p.score is not None else (f"{p.predicted_days} days" if p.predicted_days else None)
        entries.append(
            PredictionItem(
                kind=str(p.kind),
                status=str(p.status),
                band=p.band,
                score_text=text,
                model_version=p.model_version,
            )
        )
    return ToolOutcome(
        PredictionOut(entries=entries),
        refs=((args.subject_kind, args.subject_id),),
        summary=f"{len(entries)} predictions",
    )


register_tool(
    ToolSpec(
        key="predictions.get",
        mcp_exposed=True,
        version=1,
        owner_epic=OWNER_EPIC,
        title="Read predictions",
        description=(
            "Read the stored predictions (time to fill, offer drop, attrition) of one demand, offer or placement you "
            "can see, with band and model version. A prediction is a supporting signal, never a decision; this tool "
            "does not compute new ones."
        ),
        input_model=PredictionIn,
        output_model=PredictionOut,
        permissions=("prediction:read",),
        risk="read",
        scope=ScopeRule(resource="none", cross_subject=True),
        handler=_get,
    )
)
