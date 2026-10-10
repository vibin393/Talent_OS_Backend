"""``hrms`` connector category (Epic 66 contract; Epic 71 fills real adapters). Importing the package registers the
``fake_hrms`` manifest (Fake + Manual adapters; also bridged as the ``fake`` / ``manual`` provider keys)."""

from app.services.connectors.hrms import fake, hooks, manual, port

__all__ = ["fake", "hooks", "manual", "port"]
