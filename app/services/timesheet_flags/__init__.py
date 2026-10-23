# ruff: file-ignore[line-too-long]
"""Timesheet flags and nudges (Epic 63, FR-311 / FR-312). A Phase 4 domain (layer 4 of ``p4-dag``): it may import P2
facades / models, ``core`` and ``agent_runtime_service``; never the human-only ``time.timesheets`` /
``time.leave_requests`` modules. Callers use ``timesheet_flags_service``."""
