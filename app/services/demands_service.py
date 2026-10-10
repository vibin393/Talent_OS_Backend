"""Facade of the ``demands`` domain for other domains (Epic 36 contract layer, AD-39).

``create_draft`` is the only writer of ``demands.workforce_plan_line_id`` / ``project_role_id`` (Epics 44 / 45).
Signature frozen in ``tests/contracts/signatures.json``.
"""

from app.services.demands.records import create_draft

__all__ = ["create_draft"]
