# ruff: file-ignore[line-too-long]
"""Sourcing & Matching Agent domain (Epic 56, FR-285..FR-288). Phase 4 domain package ("p4 domains" layer): it owns
the agent shortlist state the ``sourcing`` agent writes through its tools (the agent package never touches models,
AD-56). Public entry point: ``app.services.sourcing_agent_service``."""
