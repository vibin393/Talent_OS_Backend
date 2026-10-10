"""``tds_statement`` connector category (AD-29; owned by Epic 51, Finance TDS reconciliation; Epic 36
contract). Importing the package registers the ``manual`` adapter, and ``fake`` outside production. Port
signatures are frozen (spec 0.8)."""

from app.services.connectors.tds_statement import fake, manual, port

__all__ = ["fake", "manual", "port"]
