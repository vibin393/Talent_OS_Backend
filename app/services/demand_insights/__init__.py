# ruff: file-ignore[line-too-long]
"""Demand insights domain (Epic 55, stories 55.2-55.5): field provenance, skill / contract / rate-card suggestions,
the deterministic duplicate scorer and flags, SLA risk and the JD wrappers. A second package next to
``demand_intake`` so intake stays focused; both sit in the ``p4-dag`` "p4 domains" layer.

Other packages import ``app.services.demand_insights_service`` (the facade)."""
