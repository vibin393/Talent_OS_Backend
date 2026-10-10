"""Expenses domain (Epic 27, FR-137..FR-139).

Modules: ``policies``, ``lines``, ``amounts`` (pure), ``claims``, ``receipts``, ``approval``,
``posting_source``, ``portal``, ``events``, ``hooks`` and the ``service`` contract (re-exported by
``app.services.expenses_service``). Sits beside ``time`` above ``placements`` (AD-32) and never
imports ``time``.
"""
