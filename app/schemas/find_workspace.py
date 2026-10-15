"""Public "find my workspace" schemas (Phase 2, Story 1.2).

Enumeration-safe: the request carries only an email and the response is the same fixed ``{status: "ok"}``
regardless of whether the email matches any tenant, so neither the email nor any tenant is ever disclosed.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, StringConstraints


class FindWorkspaceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Annotated[EmailStr, StringConstraints(max_length=254)]


class FindWorkspaceOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Always "ok": a constant response is the whole point (no account / tenant enumeration).
    status: Literal["ok"] = "ok"
