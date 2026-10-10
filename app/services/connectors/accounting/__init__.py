"""``accounting`` connector category (AD-29; owned by Epic 30 after the foundation). Importing the package
registers the ``manual`` adapter (and ``fake`` outside production)."""

from app.services.connectors.accounting import fake, manual, port

__all__ = ["fake", "manual", "port"]
