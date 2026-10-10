# ruff: noqa: E501
"""The ``collections`` agent (Epic 63, FR-314 / FR-315): spec, kickoff and registration. Imports only ``app.core`` and
the Phase 4 facade ``collection_rules_service`` (lazily, for the owner); never the database, models or P1-P3 packages
(``p4-agents-isolation``)."""
