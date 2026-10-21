"""Self-serve company signup schemas (unified platform onboarding).

A new company signs up on the public surface; the platform provisions its workspace across the apps
and returns the admin's invitation link. Local-first: there is no real email, so the response RETURNS
the invite link (the success page shows it) instead of emailing it.

(Named ``company_signup`` to avoid colliding with ``app.schemas.onboarding`` — the Epic 23 digital
onboarding schemas, an unrelated feature.)
"""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, StringConstraints

# Slug: 2-40 chars, lowercase letters / digits / hyphens (the local silo DB suffix + the subdomain label).
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]{2,40}$", to_lower=True, strip_whitespace=True)]
Name = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]


class SignupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: Name
    slug: Slug
    admin_email: Annotated[EmailStr, StringConstraints(max_length=254)]
    admin_name: Name


class SignupOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_slug: str
    admin_email: str
    # Local-first stand-in for the invitation email: the link the admin opens to set their password.
    invite_url: str


class CompanyCreateIn(SignupIn):
    """Operator "create company" body — same shape as self-serve signup (company name, slug, admin)."""


class CompanyAdminOut(BaseModel):
    """A company row for the super-admin console: a Tenant enriched with its plan and admin email.

    Covers EVERY company on the platform — operator-provisioned and self-serve signups alike (they are all
    ``Tenant`` rows in the platform DB). ``plan`` / ``admin_email`` are best-effort (null if unavailable).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    status: str
    created_at: datetime
    plan: str | None = None
    admin_email: str | None = None
