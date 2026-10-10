# ruff: noqa: E501
"""AI governance console read models (Epic 65, spec-p4-governance A1-A3, A8).

Deviation recorded: the spec names this package ``agent_runtime/admin/``, but the foundation already ships the
module ``agent_runtime/admin.py`` (enablement / configuration / kill switch) and the foundation contract is frozen,
so the console read models live in ``agent_runtime/governance/``. They read only the runtime tables, ``ai_actions``
and Epic 65's own ``proposal_review_signals``; Copilot content is never returned (coordinator decision 6, C-14).
"""
