"""Legal Entities domain (Epic 13, AD-17 / AD-18 / AD-22): entities, GST registrations, LUTs, bank accounts,
numbering series (``numbering``), accounting periods (``periods``) and FX rates (``fx``). Public API through the
facade ``app.services.legal_entities_service``."""

from app.services.legal_entities import fx, gstin, numbering, periods, service

__all__ = ["fx", "gstin", "numbering", "periods", "service"]
