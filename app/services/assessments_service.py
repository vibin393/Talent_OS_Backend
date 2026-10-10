"""Facade of the ``assessments`` domain (Epic 36 contract layer, AD-39; filled by Epic 40).

Contract (frozen): ``invite``, ``get_invitation``. Owner functions consumed by Epic 52 (foundation 0.7):
``quick_search`` (QuickHit type ``assessment``) and ``import_assessment_item`` (go-live import, FR-263).
"""

from app.services.assessments.assembly import quick_search
from app.services.assessments.item_import import import_assessment_item
from app.services.assessments.service import get_invitation, invite

__all__ = ["get_invitation", "import_assessment_item", "invite", "quick_search"]
