# ruff: file-ignore[line-too-long]
"""HRMS eligibility (Epic 71, story 71.2, spec A4; rule A3): only Workers on the Tenant's own payroll for the Legal
Entity are pushed. Vendor-supplied Workers never; ``include_override`` (per Placement) wins for own-payroll payees."""

from app.models.hrms_sync import HrmsSettings, HrmsSyncRecord
from app.models.placements import Placement

__all__ = ["eligible"]


def eligible(p: Placement, settings: HrmsSettings, record: HrmsSyncRecord | None) -> tuple[bool, str | None]:
    """``(eligible, reason)``; reason ``not_on_payroll`` when the Worker is not on the Tenant's payroll."""
    if p.payee_kind != "worker" or p.worker_type == "vendor_contractor":
        return False, "not_on_payroll"
    override = record.include_override if record is not None else None
    if override is not None:
        return bool(override), None if override else "not_on_payroll"
    if p.worker_type is None or p.worker_type not in (settings.push_worker_types or []):
        return False, "not_on_payroll"
    return True, None
