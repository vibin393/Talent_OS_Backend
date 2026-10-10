# ruff: noqa: E501
"""Compliance requirements & credentials (Epic 21, PRD group 8, FR-110/115/116, NFR-24).

Modules: ``requirements`` (configuration), ``evaluation`` (pure matcher), ``credentials``, ``expiry`` (pure),
``checklist`` (per-Placement checklist, flags, gate resolver), ``recompute`` (daily beat + alerts),
``dashboard`` (+ ``worker_compliance``), ``portal`` (Worker), ``hooks`` (registrations). Never imports ``bgv``,
``onboarding`` or ``time``; non-compliance reaches placements only through events (AD-25).
"""

from app.services.compliance.dashboard import worker_compliance

__all__ = ["worker_compliance"]
