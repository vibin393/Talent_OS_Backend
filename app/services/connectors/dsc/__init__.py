"""``dsc`` connector category (AD-29; owned by Epic 51, Finance DSC signing; Epic 36 contract). Importing the
package registers the ``manual`` adapter, and ``fake`` outside production. Port signatures are frozen (spec
0.8)."""

from app.services.connectors.dsc import fake, manual, port

__all__ = ["fake", "manual", "port"]
