"""``whatsapp`` connector category (AD-29; owned by Epic 33 after the foundation). Importing the package
registers the ``manual`` and ``meta_cloud`` adapters (and ``fake`` outside production)."""

from app.services.connectors.whatsapp import fake, manual, meta_cloud, port

__all__ = ["fake", "manual", "meta_cloud", "port"]
