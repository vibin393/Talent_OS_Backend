# ruff: noqa: E501
"""The ``billing`` agent (Epic 63, FR-313): spec, kickoff and registration. Imports only ``app.core`` and the Phase 4
facade ``collection_rules_service`` (lazily, for the schedule owner); never the database, models or P1-P3 packages
(``p4-agents-isolation``)."""
