# ruff: noqa: E501
"""Facade of the ``submission_packs`` domain (Epic 53 contract layer, spec 0.12; filled by Epic 61). Other domains call
these, never the package internals.

* ``prepare_pack`` -- the frozen contract (Epic 54's Copilot wraps it).
* Packs: ``get_detail``, ``get_open_pack``, ``list_packs``, ``run_checks``, ``save_write_up``, ``set_rates``, ``patch``,
  ``mark_opened``, ``discard``, ``link_submission``, ``metrics``.
* Agent support: ``pack_context``, ``save_write_up_for_application``, ``request_consent``, ``application_owner``.
* Client rules: ``get_client_rules`` / ``put_client_rules`` / ``effective_rules``.
"""

from app.services.submission_packs.rules import effective_rules, get_client_rules, put_client_rules
from app.services.submission_packs.service import (
    agent_status,
    application_owner,
    discard,
    get_detail,
    get_open_pack,
    link_submission,
    list_packs,
    mark_opened,
    metrics,
    pack_context,
    patch,
    prepare,
    prepare_pack,
    recheck_for_candidate,
    request_consent,
    run_checks,
    save_write_up,
    save_write_up_for_application,
    set_rates,
    stub_out,
)

__all__ = [
    "agent_status",
    "application_owner",
    "discard",
    "effective_rules",
    "get_client_rules",
    "get_detail",
    "get_open_pack",
    "link_submission",
    "list_packs",
    "mark_opened",
    "metrics",
    "pack_context",
    "patch",
    "prepare",
    "prepare_pack",
    "put_client_rules",
    "recheck_for_candidate",
    "request_consent",
    "run_checks",
    "save_write_up",
    "save_write_up_for_application",
    "set_rates",
    "stub_out",
]
