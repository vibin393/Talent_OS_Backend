# ruff: file-ignore[line-too-long]
"""The ``submission`` agent (Epic 61, FR-305..FR-307): spec, triggers, kickoff and registration. Imports only
``app.core``, ``app.schemas.agentic`` and the Phase 4 facade ``submission_packs_service`` (lazily, for the owner); never
the database, models or P1-P3 packages (``p4-agents-isolation``)."""
