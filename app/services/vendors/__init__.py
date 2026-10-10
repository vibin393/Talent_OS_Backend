"""Vendors domain (Epic 17, FR-93..FR-99): vendor records and status, documents, releases, vendor submissions,
review / duplicates / ownership, vendor-facing progress, performance and the Vendor Portal content.

Modules: ``records``, ``releases``, ``submissions``, ``review``, ``progress``, ``performance``, ``portal``
(the
only module the vendor-portal routers call), ``notice``, ``hooks``. ``service`` holds the AD-36 contract
functions re-exported here and by ``app.services.vendors_service``.
"""

from app.services.vendors import service
from app.services.vendors.service import (
    assert_vendor_can_submit,
    get_vendor_summary,
    sourcing_for_application,
)

__all__ = ["assert_vendor_can_submit", "get_vendor_summary", "service", "sourcing_for_application"]
