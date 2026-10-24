# ruff: file-ignore[line-too-long]
"""Indeed adapter (Epic 70, spec A2; Indeed Job Sync API + Indeed Apply, live mode only under a partner agreement).

* OAuth 2 client credentials at ``https://apis.indeed.com/oauth/v2/tokens`` (scope ``employer_access``), per employer.
* ``publish`` / ``update``: GraphQL mutation ``jobsIngest.createSourcedJobPostings`` at
  ``https://apis.indeed.com/graphql``; salary in minor units (paise) only when the posting shows it; ``applyMethod``
  Indeed Apply (``postUrl`` = our connector webhook) or the careers site (``src=indeed``). Returns
  ``sourcedPostingId`` (the ``board_ref``).
* ``close``: ``jobsIngest.expireSourcedJobsBySourcedPostingId``; ``status``: ``jobData.sourcedJobPostings`` by id
  (Indeed reports no credits).
* Indeed Apply webhook: ``X-Indeed-Signature`` = base64(HMAC-SHA1(body, Indeed Apply API secret)); verified in
  constant time. The secret is the manifest's ``webhook_secret`` field (labelled "Indeed Apply API secret").
  ``applicant_from_payload`` maps the push (resume base64 included) to a ``BoardApplicantDTO``.
"""

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.services.connectors.job_board.live_base import (
    LiveJobBoardBase,
    classify,
    description_subset,
    parse_dt,
    sanitise_message,
)
from app.services.connectors.job_board.port import (
    BoardApplicantDTO,
    BoardFieldDTO,
    BoardRefDTO,
    FieldSchemaDTO,
    JobPostingDTO,
)

__all__ = [
    "GRAPHQL_URL",
    "SIGNATURE_HEADER",
    "TOKEN_URL",
    "IndeedAdapter",
    "IndeedConfig",
    "applicant_from_payload",
    "job_posting_input",
    "sign",
]

GRAPHQL_URL = "https://apis.indeed.com/graphql"
TOKEN_URL = "https://apis.indeed.com/oauth/v2/tokens"
SIGNATURE_HEADER = "X-Indeed-Signature"
ALLOWED_TAGS = frozenset({"p", "ul", "ol", "li", "b", "strong", "i", "em", "br", "h3"})
_PERIOD = {"hour": "HOUR", "day": "DAY", "month": "MONTH", "year": "YEAR"}
FIELDS = FieldSchemaDTO(
    fields=(
        BoardFieldDTO("category", "Job category", True, "text"),
        BoardFieldDTO("benefits", "Benefits", False, "text"),
    )
)
CREATE_MUTATION = (
    "mutation CreateSourcedJobPostings($input: CreateSourcedJobPostingsInput!) { jobsIngest { "
    "createSourcedJobPostings(input: $input) { results { jobPosting { sourcedPostingId } } } } }"
)
EXPIRE_MUTATION = (
    "mutation Expire($input: ExpireSourcedJobsBySourcedPostingIdInput!) { jobsIngest { "
    "expireSourcedJobsBySourcedPostingId(input: $input) { results { trackingKey } } } }"
)
STATUS_QUERY = (
    "query Status($ids: [ID!]!) { jobData { sourcedJobPostings(sourcedPostingIds: $ids) { results { "
    "sourcedPostingId status url expirationDate } } } }"
)


class IndeedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employer_id: str = ""
    apply_mode: Literal["indeed_apply", "careers_site"] = "indeed_apply"


def sign(secret: str, body: bytes) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha1).digest()).decode()  # NOSONAR


def _minor(amount: Decimal | None) -> int | None:
    return int(amount * 100) if amount is not None else None


def job_posting_input(posting: JobPostingDTO, config: IndeedConfig, *, post_url: str | None, company: str | None) -> dict[str, Any]:
    loc = posting.location
    body: dict[str, Any] = {
        "title": posting.title[:200],
        "description": description_subset(posting.description_html, ALLOWED_TAGS),
        "location": {"country": (loc.country if loc else "IN") or "IN", "cityRegionPostal": ", ".join(p for p in ((loc.city, loc.region) if loc else ()) if p)},
    }
    if posting.board_fields.get("benefits"):
        body["benefits"] = str(posting.board_fields["benefits"])[:2000]
    if posting.salary is not None:
        body["salary"] = {
            "minimumMinor": _minor(posting.salary.min_amount),
            "maximumMinor": _minor(posting.salary.max_amount),
            "period": _PERIOD.get(posting.salary.period, "YEAR"),
            "currency": posting.salary.currency or "INR",
        }
    metadata: dict[str, Any] = {
        "jobSource": {"companyName": company or posting.company_name or "", "sourceName": company or "", "sourceType": "Employer"},
        "jobPostingId": str(posting.posting_id),
        "jobRequisitionId": str(posting.board_fields.get("requisition_id") or posting.demand_id),
        "datePublished": posting.board_fields.get("date_published") or "",
        "url": posting.apply_url,
        "contacts": [],
        "taxonomyClassification": {"category": posting.board_fields.get("category")},
    }
    apply: dict[str, Any]
    if config.apply_mode == "indeed_apply" and post_url:
        apply = {"indeedApply": {"postUrl": post_url}}
    else:
        apply = {"careersSite": {"url": posting.apply_url}}
    return {"body": body, "metadata": metadata, "applyMethod": apply}


def applicant_from_payload(payload: Mapping[str, Any]) -> BoardApplicantDTO:
    """Map an Indeed Apply push to the port DTO (resume decoded; untrusted text kept as data)."""
    applicant = dict(payload.get("applicant") or {})
    job = dict(payload.get("job") or {})
    resume = dict((applicant.get("resume") or {}).get("file") or {})
    data: bytes | None = None
    raw = resume.get("data")
    if isinstance(raw, str) and raw and not raw.startswith("<stored:"):
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            data = None
    questions = ((payload.get("questions") or {}).get("questions")) or []
    answers = tuple(
        (str(q.get("question") or q.get("id") or "")[:300], str(q.get("answer") or "")[:2000])
        for q in questions
        if isinstance(q, Mapping)
    )
    return BoardApplicantDTO(
        board_application_id=str(payload.get("id") or ""),
        board_ref=str(job.get("jobId") or job.get("sourcedPostingId") or ""),
        applied_at=parse_dt(payload.get("appliedOnMillis")) or parse_dt(payload.get("appliedOn")) or parse_dt(0),  # type: ignore[arg-type]
        full_name=str(applicant.get("fullName") or "").strip()[:200] or "Indeed applicant",
        email=applicant.get("email") or None,
        phone_e164=applicant.get("phoneNumber") or None,
        resume=data,
        resume_filename=resume.get("fileName") if data else None,
        resume_media_type=resume.get("contentType") if data else None,
        answers=answers,
        location=applicant.get("location") or None,
        current_title=applicant.get("currentTitle") or None,
        experience_years=None,
    )


class IndeedAdapter(LiveJobBoardBase):
    provider_key = "indeed"

    @property
    def config(self) -> IndeedConfig:
        cfg = self.ctx.config
        return cfg if isinstance(cfg, IndeedConfig) else IndeedConfig.model_validate(cfg.model_dump())

    async def _token(self) -> str:
        resp = await self.http.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials", "scope": "employer_access",
                "client_id": self.secret("client_id"), "client_secret": self.secret("client_secret"),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )  # fmt: skip
        classify(resp, "token")
        return str((resp.json() or {}).get("access_token") or "")

    async def _graphql(self, query: str, variables: Mapping[str, Any], what: str) -> Mapping[str, Any]:
        resp = await self.http.post(
            GRAPHQL_URL,
            headers={"Authorization": f"Bearer {await self._token()}", "Content-Type": "application/json", "indeed-employer": self.config.employer_id},
            json={"query": query, "variables": dict(variables)},
        )
        classify(resp, what)
        data = resp.json() or {}
        errors = data.get("errors") or []
        if errors:
            from app.core.integrations.errors import ConnectorPermanentError

            raise ConnectorPermanentError("board_validation", sanitise_message(errors[0].get("message")) or "Indeed refused the request")
        return dict(data.get("data") or {})

    def _post_url(self) -> str | None:
        if self.ctx.config_id is None:
            return None
        from app.core.settings import get_settings

        return f"{get_settings().p5_public_webhook_base_url()}/api/v1/hooks/job_board/{self.ctx.config_id}"

    async def health(self) -> None:
        await self._token()

    async def _ingest(self, posting: JobPostingDTO) -> BoardRefDTO:
        item = job_posting_input(posting, self.config, post_url=self._post_url(), company=posting.company_name)
        data = await self._graphql(CREATE_MUTATION, {"input": {"jobPostings": [item]}}, "publish")
        results = (((data.get("jobsIngest") or {}).get("createSourcedJobPostings") or {}).get("results")) or []
        ref = str(((results[0] if results else {}).get("jobPosting") or {}).get("sourcedPostingId") or "")
        if not ref:
            from app.core.integrations.errors import ConnectorPermanentError

            raise ConnectorPermanentError("board_validation", "Indeed returned no posting id")
        return BoardRefDTO(ref, None, "live", None, posting.expires_at, None, None, None)

    async def publish(self, posting: JobPostingDTO) -> BoardRefDTO:
        return await self._ingest(posting)

    async def update(self, board_ref: str, posting: JobPostingDTO) -> BoardRefDTO:  # ruff: ignore[unused-method-argument] - re-ingest by jobPostingId
        return await self._ingest(posting)

    async def close(self, board_ref: str) -> BoardRefDTO:
        await self._graphql(EXPIRE_MUTATION, {"input": {"sourcedPostingIds": [board_ref]}}, "close")
        return BoardRefDTO(board_ref, None, "closed", None, None, None, None, None)

    async def status(self, board_ref: str) -> BoardRefDTO:
        data = await self._graphql(STATUS_QUERY, {"ids": [board_ref]}, "status")
        results = (((data.get("jobData") or {}).get("sourcedJobPostings") or {}).get("results")) or []
        row: Mapping[str, Any] = results[0] if results else {}
        raw = str(row.get("status") or "").upper()
        status = {"ACTIVE": "live", "LIVE": "live", "EXPIRED": "expired", "CLOSED": "closed", "REJECTED": "rejected"}.get(raw, "pending")
        return BoardRefDTO(board_ref, row.get("url") or None, status, None, parse_dt(row.get("expirationDate")), None, None, None)  # type: ignore[arg-type]

    async def field_schema(self) -> FieldSchemaDTO:
        return FIELDS

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        secret = self.ctx.secrets.get("webhook_secret")
        if not secret:
            return False
        given = next((v for k, v in headers.items() if k.lower() == SIGNATURE_HEADER.lower()), "")
        return bool(given) and hmac.compare_digest(given.strip(), sign(secret, body))

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # ruff: ignore[unused-method-argument]
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
