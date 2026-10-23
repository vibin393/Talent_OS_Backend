# ruff: file-ignore[line-too-long]
"""Sourcing & Matching Agent facade (Epic 56; AD-1 naming). The entry point of the ``sourcing_agent`` domain for the
router, tasks and the Epic 56 agent tools (``agent_tools.{matching,search,talent_pools,pipeline,campaigns,sourcing}``).
Epic 61 may read ``get_current`` (soft dependency)."""

from app.services.sourcing_agent.contactability import contactability, do_not_contact_ids
from app.services.sourcing_agent.operations import (
    attach_proposal,
    cap_status,
    current_entry_for,
    get_for_demand,
    prepare_draft,
    save_suggested,
    send_outreach,
)
from app.services.sourcing_agent.outreach import PROTECTED_TERMS, protected_terms, validate_draft
from app.services.sourcing_agent.ranking import rank
from app.services.sourcing_agent.service import (
    agent_enabled,
    build_candidate_set,
    candidate_facts,
    demand_facts,
    engagement,
    get_settings_,
    posting_url,
    update_settings,
)
from app.services.sourcing_agent.shortlists import (
    add_via_proposal,
    decide_entry,
    entry_version,
    get_current,
    get_version,
    list_versions,
)
from app.services.sourcing_agent.triggers import start_manual_run

get_settings = get_settings_

__all__ = [
    "PROTECTED_TERMS",
    "add_via_proposal",
    "agent_enabled",
    "attach_proposal",
    "build_candidate_set",
    "candidate_facts",
    "cap_status",
    "contactability",
    "current_entry_for",
    "decide_entry",
    "demand_facts",
    "do_not_contact_ids",
    "engagement",
    "entry_version",
    "get_current",
    "get_for_demand",
    "get_settings",
    "get_version",
    "list_versions",
    "posting_url",
    "prepare_draft",
    "protected_terms",
    "rank",
    "save_suggested",
    "send_outreach",
    "start_manual_run",
    "update_settings",
    "validate_draft",
]
