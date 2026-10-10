"""Sandbox Tenants (Epic 66, Story 66.9, FR-339, AD-85): creation (``tenants``) and the deterministic synthetic seed
(``seed``). The enforcement points live where the behaviour is (``core.tenancy.is_sandbox`` is the single check):
connectors (Fake / Manual only), ``core.mailer`` (outbox only, no mailbox routing), ``ai.gateway`` / ``ai.loop`` (Fake
LLM), ``core.middleware.sandbox`` (``X-TalentOS-Sandbox`` header + ``sandbox_last_active_at``), ``imports_service`` and
the candidate bulk import (1,000-record cap). Public API: ``app.services.sandbox_service``."""
