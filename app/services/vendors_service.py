"""Facade of the ``vendors`` domain (Epic 13 contract layer; filled by Epic 17)."""

from app.services.vendors.service import (
    assert_vendor_can_submit,
    candidate_vendor_owner,  # Epic 61 (additive read)
    get_vendor_summary,
    sourcing_for_application,
)

__all__ = [
    "assert_vendor_can_submit",
    "candidate_vendor_owner",
    "get_vendor_summary",
    "sourcing_for_application",
]
