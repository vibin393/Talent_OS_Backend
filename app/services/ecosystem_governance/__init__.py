# ruff: file-ignore[line-too-long]
"""Epic 79 (Phase 5 Governance): the ecosystem console, access reviews, data-flow register, violations,
erasure fan-out, enablement checks, go-live checks, sandbox purge, dashboards and the release gate.

Top layer above ``p5-dag`` layer 1: reads every Phase 5 facade read-only; nothing imports it (``p5-human-only``).
"""
