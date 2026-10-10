"""Phase 4 package ``recording_policy`` (Epic 53 table + gate, FR-294; Epic 58 owns the settings UI / API).

Public API (``gate``): ``GateDecision``, ``check``, ``seed_tenant_defaults``.
"""

from app.services.recording_policy.gate import GateDecision, check, seed_tenant_defaults

__all__ = ["GateDecision", "check", "seed_tenant_defaults"]
