# ruff: noqa: E501
"""Epic 54 Copilot tool ``navigation.screen_for`` (FR-278, FR-280): where a person does an action the Copilot must not
do. ★ actions (reject, shortlist, submit, release an offer, record an interview outcome, approve, decide a proposal,
change settings, export) are never tools; the Copilot explains and deep-links. A static map; a deep link to a record is given only when you can see the record."""

import uuid
from typing import Any, Literal

from app.core.agentic import ScopeRule, ToolOutcome, ToolSpec, register_tool
from app.services import entity_refs
from app.services.agent_tools.copilot.common_e54 import OWNER_EPIC, In, Out, scoped, url_for

Action = Literal[
    "reject_application", "shortlist", "submit_to_client", "release_offer", "record_interview_outcome", "approve",
    "decide_proposal", "change_settings", "export", "open_demand", "publish_posting", "other",
]  # fmt: skip
EntityType = Literal[
    "candidate", "demand", "application", "client", "placement", "interview", "offer", "project", "worker"
]

HUMAN = "Only a person can do this in Talent OS; AI never does it for you."
# action -> (label, fallback url, reason, preferred entity type for the deep link)
ACTION_SCREENS: dict[str, tuple[str, str, str, str | None]] = {
    "reject_application": (
        "Open the pipeline",
        "/demands",
        f"Rejecting a candidate is a hiring decision. {HUMAN}",
        "demand",
    ),
    "shortlist": (
        "Open the pipeline",
        "/demands",
        f"Shortlisting moves a candidate forward. {HUMAN}",
        "demand",
    ),
    "submit_to_client": (
        "Open the demand",
        "/demands",
        f"Submitting a candidate to a client is a person's decision. {HUMAN}",
        "demand",
    ),
    "release_offer": (
        "Open offers",
        "/offers",
        f"Releasing an offer is a commitment to a candidate. {HUMAN}",
        None,
    ),
    "record_interview_outcome": (
        "Open the interview",
        "/interviews",
        f"Interview outcomes and scorecards come from the interviewers. {HUMAN}",
        "interview",
    ),
    "approve": ("Open approvals", "/approvals", f"Approvals are decided by the approver. {HUMAN}", None),
    "decide_proposal": (
        "Open approvals",
        "/approvals",
        f"AI proposals are approved or rejected by a person. {HUMAN}",
        None,
    ),
    "change_settings": (
        "Open settings",
        "/admin",
        "Settings are changed by an administrator, not by Copilot.",
        None,
    ),
    "export": (
        "Open reports",
        "/reports/applications",
        "Exports are run by a person with export access.",
        None,
    ),
    "open_demand": (
        "Open the demand",
        "/demands",
        "Opening or publishing a demand is done on the demand screen.",
        "demand",
    ),
    "publish_posting": (
        "Open the demand",
        "/demands",
        "Postings are published by a person from the demand screen.",
        "demand",
    ),
    "other": (
        "Open Talent OS",
        "/dashboard",
        "Copilot can't do this; you can do it yourself in Talent OS.",
        None,
    ),
}


class ScreenForIn(In):
    action: Action
    entity_type: EntityType | None = None
    entity_id: uuid.UUID | None = None


class ScreenForOut(Out):
    label: str
    url: str
    reason: str


async def _screen_for(tctx: Any, args: ScreenForIn) -> ToolOutcome:
    label, fallback, reason, preferred = ACTION_SCREENS[args.action]
    url = fallback
    refs: tuple[tuple[str, uuid.UUID], ...] = ()
    if args.entity_type and args.entity_id:
        link = url_for(args.entity_type, args.entity_id)
        if (
            link is not None
            and (
                preferred is None
                or args.entity_type == preferred
                or args.entity_type in ("candidate", "interview")
            )
            and await entity_refs.can_read(scoped(tctx), tctx.db, args.entity_type, args.entity_id)
        ):
            url = link
            refs = ((args.entity_type, args.entity_id),)
    return ToolOutcome(
        ScreenForOut(label=label, url=url, reason=reason), refs=refs, summary="found the screen"
    )


register_tool(
    ToolSpec(
        key="navigation.screen_for",
        mcp_exposed=True,
        version=1,
        owner_epic=OWNER_EPIC,
        title="Found the screen",
        description=(
            "For an action Copilot must not do (reject, shortlist, submit to a client, release an offer, record an "
            "interview outcome, approve, decide a proposal, change settings, export, open a demand, publish a posting), "
            "return the screen where the person can do it and why. It performs nothing."
        ),
        input_model=ScreenForIn,
        output_model=ScreenForOut,
        permissions=(),
        risk="read",
        scope=ScopeRule(resource="none", cross_subject=True),
        handler=_screen_for,
    )
)
