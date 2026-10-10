# ruff: noqa: E501
"""Interview Assistant domain (Epic 60, FR-301..FR-304). Phase 4 domain package ("p4 domains" layer): consent and
Recording Policy gating of interview captures, uploads, the assistant's outputs and private draft scorecards, interview
skill suggestions, withdrawal and retention. The ``interview_assist`` agent package never touches models (AD-56); it
reaches this domain through its tools. Public entry point: ``app.services.interview_captures_service``."""
