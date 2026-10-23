# ruff: file-ignore[line-too-long]
"""Facade of the ``demand_intake`` domain (Epic 55, A5). Routers, tasks, agent tools and the Demand Agent's owner
callables import from here (Phase 4 domain layer, ``p4-dag``)."""

from app.services.demand_intake.extraction import extract
from app.services.demand_intake.items import (
    accountable_owner,
    assign_client,
    create_from_upload,
    discard,
    finalize_from_run,
    get_for_agent,
    get_message,
    list_messages,
    match_of,
    matched_client,
    purge_expired,
    record_drafts,
    retry,
)
from app.services.demand_intake.mailbox import poll_connected_mailbox
from app.services.demand_intake.routes import receive_platform_email
from app.services.demand_intake.settings import (
    add_sender_domain,
    get_agent_settings,
    get_intake_settings,
    list_sender_domains,
    remove_sender_domain,
    rotate_address,
    update_agent_settings,
    update_intake_settings,
)
from app.services.demand_intake.settings import row as settings_row

get_settings = get_intake_settings
update_settings = update_intake_settings

__all__ = [
    "accountable_owner",
    "add_sender_domain",
    "assign_client",
    "create_from_upload",
    "discard",
    "extract",
    "finalize_from_run",
    "get_agent_settings",
    "get_for_agent",
    "get_intake_settings",
    "get_message",
    "get_settings",
    "list_messages",
    "list_sender_domains",
    "match_of",
    "matched_client",
    "poll_connected_mailbox",
    "purge_expired",
    "receive_platform_email",
    "record_drafts",
    "remove_sender_domain",
    "retry",
    "rotate_address",
    "settings_row",
    "update_agent_settings",
    "update_intake_settings",
    "update_settings",
]
