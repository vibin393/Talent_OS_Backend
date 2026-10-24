# ruff: file-ignore[line-too-long]
"""LinkedIn Jobs adapter (Epic 70, spec A2; D-3 platform OAuth app, D-4 posting only -- apply on the careers site).

API (LinkedIn Talent Solutions Job Posting API, versioned REST; recorded fixtures pin the shape):

* token: the tenant's refresh token (vault, ``oauth_refresh``) exchanged at
  ``https://www.linkedin.com/oauth/v2/accessToken`` with the **platform** client id / secret. Adapters cannot import
  the vault: the job distribution hooks register ``set_platform_credentials(fn)`` (``vault_service.platform_get``).
* ``publish`` / ``update`` / ``close``: POST ``/rest/simpleJobPostings`` (``LinkedIn-Version``, ``X-Restli-Method:
  batch_create``) with one element whose ``jobPostingOperationType`` is ``CREATE | UPDATE | CLOSE``;
  ``externalJobPostingId`` = the Board Posting id (LinkedIn keys postings by it, so it is the ``board_ref``). The call
  is asynchronous: the result is ``pending`` until ``status`` reads the task.
* ``status``: GET ``/rest/simpleJobPostingTasks?q=externalJobPostingId`` -> ``SUCCEEDED`` (live, job URL) / ``FAILED``
  (rejected with the message) / in progress (pending).
"""

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.integrations.errors import ConnectorAuthError
from app.services.connectors.job_board.live_base import (
    LiveJobBoardBase,
    classify,
    description_subset,
    sanitise_message,
)
from app.services.connectors.job_board.port import BoardFieldDTO, BoardRefDTO, FieldSchemaDTO, JobPostingDTO

__all__ = [
    "API_BASE",
    "LINKEDIN_VERSION",
    "TOKEN_URL",
    "LinkedInAdapter",
    "LinkedInConfig",
    "element",
    "employment_status",
    "experience_level",
    "set_platform_credentials",
]

API_BASE = "https://api.linkedin.com/rest"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
LINKEDIN_VERSION = "202409"
ALLOWED_TAGS = frozenset({"p", "ul", "ol", "li", "b", "strong", "i", "em", "br"})
FIELDS = FieldSchemaDTO(fields=(BoardFieldDTO("job_function", "Job function", False, "text"),))

PlatformCredentials = Callable[[], Awaitable[tuple[str, str]]]
_platform_credentials: PlatformCredentials | None = None


def set_platform_credentials(fn: PlatformCredentials | None) -> None:
    """``fn() -> (client_id, client_secret)`` of the platform LinkedIn app (registered by the job distribution hooks)."""
    global _platform_credentials  # process-wide registration
    _platform_credentials = fn


class LinkedInConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_urn: str = ""
    contract_id: str | None = None


def employment_status(employment_type: str) -> str:
    return {"permanent": "FULL_TIME", "contract": "CONTRACT", "contract_to_hire": "CONTRACT", "freelance": "TEMPORARY"}.get(employment_type, "FULL_TIME")


def experience_level(min_years: Decimal | None) -> str:
    years = int(min_years or 0)
    if years <= 1:
        return "ENTRY_LEVEL"
    if years <= 5:
        return "ASSOCIATE"
    if years <= 10:
        return "MID_SENIOR_LEVEL"
    return "DIRECTOR"


_WORKPLACE = {"onsite": "On-site", "hybrid": "Hybrid", "remote": "Remote"}


def element(posting: JobPostingDTO, config: LinkedInConfig, operation: str, listed_at_ms: int) -> dict[str, Any]:
    loc = posting.location
    location = ", ".join(p for p in ((loc.city, loc.region, loc.country) if loc else ()) if p) or None
    body: dict[str, Any] = {
        "integrationContext": config.organization_urn,
        "companyApplyUrl": posting.apply_url,
        "description": description_subset(posting.description_html, ALLOWED_TAGS, 25000),
        "employmentStatus": employment_status(posting.employment_type),
        "externalJobPostingId": str(posting.posting_id),
        "listedAt": listed_at_ms,
        "jobPostingOperationType": operation,
        "title": posting.title[:200],
        "workplaceTypes": [_WORKPLACE.get(posting.work_mode, "On-site")],
        "experienceLevel": experience_level(posting.experience_min_years),
    }
    if location:
        body["location"] = location
    if posting.company_name:
        body["companyName"] = posting.company_name
    return body


class LinkedInAdapter(LiveJobBoardBase):
    provider_key = "linkedin"

    @property
    def config(self) -> LinkedInConfig:
        cfg = self.ctx.config
        return cfg if isinstance(cfg, LinkedInConfig) else LinkedInConfig.model_validate(cfg.model_dump())

    async def _token(self) -> str:
        refresh = self.secret("oauth_refresh")
        if _platform_credentials is None:
            msg = "The platform LinkedIn app is not configured"
            raise ConnectorAuthError(msg)
        client_id, client_secret = await _platform_credentials()
        resp = await self.http.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id, "client_secret": client_secret},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code in (400, 401, 403):
            msg = "LinkedIn refused the connection; reconnect the company page"
            raise ConnectorAuthError(msg)
        classify(resp, "token")
        token = (resp.json() or {}).get("access_token")
        if not token:
            msg = "LinkedIn returned no access token"
            raise ConnectorAuthError(msg)
        return str(token)

    async def _headers(self, *, batch: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {await self._token()}",
            "LinkedIn-Version": LINKEDIN_VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
        }
        if batch:
            headers["X-Restli-Method"] = "batch_create"
        return headers

    async def _post(self, posting: JobPostingDTO, operation: str) -> BoardRefDTO:
        listed = int(self.ctx.now().astimezone(UTC).timestamp() * 1000)
        resp = await self.http.post(
            f"{API_BASE}/simpleJobPostings",
            headers=await self._headers(batch=True),
            json={"elements": [element(posting, self.config, operation, listed)]},
        )
        classify(resp, operation.lower())
        results = (resp.json() or {}).get("elements") or []
        first: Mapping[str, Any] = results[0] if results else {}
        if int(first.get("status") or 201) >= 400:
            return BoardRefDTO(str(posting.posting_id), None, "rejected", sanitise_message(first.get("error") or "LinkedIn refused the posting"), None, None, None, None)
        status = "closed" if operation == "CLOSE" else "pending"
        return BoardRefDTO(str(posting.posting_id), None, status, None, posting.expires_at, None, None, None)  # type: ignore[arg-type]

    async def health(self) -> None:
        await self._token()

    async def publish(self, posting: JobPostingDTO) -> BoardRefDTO:
        return await self._post(posting, "CREATE")

    async def update(self, board_ref: str, posting: JobPostingDTO) -> BoardRefDTO:  # ruff: ignore[unused-method-argument] - LinkedIn keys by externalJobPostingId
        return await self._post(posting, "UPDATE")

    async def close(self, board_ref: str) -> BoardRefDTO:
        listed = int(self.ctx.now().astimezone(UTC).timestamp() * 1000)
        resp = await self.http.post(
            f"{API_BASE}/simpleJobPostings",
            headers=await self._headers(batch=True),
            json={"elements": [{"integrationContext": self.config.organization_urn, "externalJobPostingId": board_ref, "jobPostingOperationType": "CLOSE", "listedAt": listed}]},
        )
        classify(resp, "close")
        return BoardRefDTO(board_ref, None, "closed", None, None, None, None, None)

    async def status(self, board_ref: str) -> BoardRefDTO:
        resp = await self.http.get(
            f"{API_BASE}/simpleJobPostingTasks",
            headers=await self._headers(),
            params={"q": "externalJobPostingId", "externalJobPostingId": board_ref},
        )
        classify(resp, "status")
        elements = (resp.json() or {}).get("elements") or []
        task: Mapping[str, Any] = elements[0] if elements else {}
        state = str(task.get("status") or "IN_PROGRESS").upper()
        if state == "SUCCEEDED":
            return BoardRefDTO(board_ref, task.get("jobPostingUrl") or None, "live", None, None, None, None, None)
        if state == "FAILED":
            return BoardRefDTO(board_ref, None, "rejected", sanitise_message(task.get("message")) or "LinkedIn rejected the posting", None, None, None, None)
        return BoardRefDTO(board_ref, None, "pending", None, None, None, None, None)

    async def field_schema(self) -> FieldSchemaDTO:  # NOSONAR
        return FIELDS
