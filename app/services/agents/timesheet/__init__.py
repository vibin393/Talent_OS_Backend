# ruff: noqa: E501
"""The ``timesheet`` agent (Epic 63, FR-311 / FR-312): spec, kickoff and registration. Imports only ``app.core`` and the
Phase 4 facade ``timesheet_flags_service`` (lazily, for the owner); never the database, models or P1-P3 packages
(``p4-agents-isolation``)."""
