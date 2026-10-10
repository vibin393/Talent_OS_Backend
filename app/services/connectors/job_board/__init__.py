"""``job_board`` connector category (Epic 66 contract; Epic 70 fills real adapters). Importing the package registers the
``fake_job_board`` manifest (Fake + Manual adapters; also bridged as the ``fake`` / ``manual`` provider keys)."""

from app.services.connectors.job_board import fake, hooks, manual, port

__all__ = ["fake", "hooks", "manual", "port"]
