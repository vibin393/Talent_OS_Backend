# ruff: file-ignore[line-too-long]
"""Collection rules, Collections Agent holds, sends and log (Epic 63, FR-313..FR-315). A Phase 4 domain (layer 4 of
``p4-dag``): it may import P2 facades / models, ``core`` and ``agent_runtime_service``; never the human-only
``receivables.payments`` / ``write_offs`` / ``periods_close`` or ``billing.issue`` / ``notes`` / ``einvoice`` modules.
Callers use ``collection_rules_service``."""
