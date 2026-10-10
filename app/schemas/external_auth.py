"""External principals' ``/me`` (Epic 13; client portal, vendor portal, worker "My Work")."""

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints


class ExternalMeOut(BaseModel):
    principal_type: Literal["client_user", "vendor_user", "candidate"]
    id: uuid.UUID
    tenant_id: uuid.UUID
    org_kind: Literal["client", "vendor", "worker"]
    org_id: uuid.UUID
    display_name: str
    portal_permissions: list[str]


# ================================================================ Epic 14: internal access administration
from datetime import datetime  # noqa: E402

from pydantic import ConfigDict, Field  # noqa: E402

from app.schemas.identity import Email  # noqa: E402

ClientPortalPermission = Literal[
    "submissions:review",
    "demands:approve",
    "time:approve",
    "placements:view",
    "invoices:view",
    # Epic 45 (FR-231)
    "projects:view",
    "deliverables:accept",
    # Epic 44 (FR-226)
    "forecasts:submit",
    # Epic 77 (FR-396)
    "portal_copilot:use",
]
# Vendor portal permission keys are defined by the Vendors epic (17); any ``area:action`` key is accepted
# here.
VendorPortalPermission = Annotated[str, StringConstraints(pattern=r"^[a-z_]+:[a-z_]+$", max_length=60)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClientUserScopeIn(_In):
    subject_kind: Literal["demand", "placement"]
    subject_id: uuid.UUID


class ClientUserGrantIn(_In):
    contact_id: uuid.UUID
    portal_permissions: list[ClientPortalPermission] = Field(default_factory=list)
    restricted: bool = False
    scopes: list[ClientUserScopeIn] = Field(default_factory=list)
    mfa_enforced: bool = False


class ClientUserUpdate(_In):
    portal_permissions: list[ClientPortalPermission] | None = None
    restricted: bool | None = None
    scopes: list[ClientUserScopeIn] | None = None
    mfa_enforced: bool | None = None


class ClientUserOut(BaseModel):
    id: uuid.UUID
    client_id: uuid.UUID
    contact_id: uuid.UUID
    email: str
    display_name: str
    status: Literal["invited", "active", "suspended", "revoked"]
    portal_permissions: list[str]
    restricted: bool
    scopes: list[ClientUserScopeIn]
    mfa_enforced: bool
    mfa_enabled: bool
    last_sign_in_at: datetime | None
    created_at: datetime


class VendorUserInviteIn(_In):
    email: Email
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    is_vendor_admin: bool = False
    portal_permissions: list[VendorPortalPermission] = Field(default_factory=list)


class VendorUserUpdate(_In):
    display_name: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None
    ) = None
    is_vendor_admin: bool | None = None
    portal_permissions: list[VendorPortalPermission] | None = None


class VendorUserOut(BaseModel):
    id: uuid.UUID
    vendor_id: uuid.UUID
    email: str
    display_name: str
    status: Literal["invited", "active", "suspended", "deactivated"]
    is_vendor_admin: bool
    portal_permissions: list[str]
    mfa_enabled: bool
    last_sign_in_at: datetime | None
    created_at: datetime
