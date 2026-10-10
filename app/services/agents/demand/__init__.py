# ruff: noqa: E501
"""The ``demand`` agent (Epic 55, FR-281..FR-284): definition, triggers, kickoff and registration. Imports only
``app.core``, ``app.events`` and the Phase 4 facades below it (``demand_intake_service`` / ``demand_insights_service``
for the owner callables); never the database, models or P1-P3 packages (``p4-agents-isolation``)."""
