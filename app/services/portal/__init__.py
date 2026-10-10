"""Portal service facade (Epic 9: Career Portal + Candidate Portal).

``from app.services import portal as portal_service``. Modules:

* ``careers`` -- public career site: branding, posting search / detail + JSON-LD, sitemap, apply form,
  posting search vector (``refresh_posting_search``).
* ``site`` -- career-site administration (branding, images, stage labels, a posting's apply questions).
* ``apply`` -- consent-first apply sessions, resume upload / parse, submit, confirmation, purge.
* ``candidate_auth`` -- magic-link sign-in, candidate sessions (``tos_candidate``), ``authenticate_candidate``.
* ``self_service`` -- profile, resumes, applications (stage labels), withdraw, interviews, offers, AI screenings.
* ``privacy_self_service`` -- consents, data export, correction / erasure requests.
* ``projection`` -- the only builder of ``Public*`` / ``Portal*`` models (allow-lists).
"""

from app.services.portal import (
    apply,
    candidate_auth,
    careers,
    common,
    handlers,
    privacy_self_service,
    projection,
    self_service,
    site,
)
from app.services.portal.common import CandidatePrincipal

handlers.register()

__all__ = [
    "CandidatePrincipal",
    "apply",
    "candidate_auth",
    "careers",
    "common",
    "handlers",
    "privacy_self_service",
    "projection",
    "self_service",
    "site",
]
