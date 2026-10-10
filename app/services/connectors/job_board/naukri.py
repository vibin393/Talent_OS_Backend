# ruff: noqa: E501
"""Naukri RMS adapter (Epic 70, spec A2; live mode only behind configuration and a signed partner agreement, D-13).

API shape (Naukri RMS job-posting / response APIs, partner documentation; recorded fixtures pin it): base
``https://api.naukri.com/rms/v1`` with the employer's ``x-api-key`` / ``x-api-secret`` and ``x-account-id`` headers.
The hosts are placeholders until the partner agreement confirms them (Known gap 1).

* ``publish`` POST ``/jobs`` -> ``{jobId, status, expiryDate}``; ``update`` PUT ``/jobs/{jobId}``; ``close`` POST
  ``/jobs/{jobId}/expire``; ``status`` GET ``/jobs/{jobId}`` (``status``, ``responses``, ``views``, ``expiryDate``,
  ``creditsLeft``).
* CTC is sent in lakhs per annum (``year`` /100,000; ``month`` x12/100,000; ``day`` / ``hour`` -> hidden).
* ``fetch_applicants(since, cursor)`` GET ``/responses?since=&page=&pageSize=50``; each CV is downloaded through a
  sibling client capped at 5 MB (larger -> no resume).
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.integrations.errors import ConnectorPermanentError
from app.core.net_safety import EgressRefused
from app.services.connectors.job_board.live_base import (
    LiveJobBoardBase,
    classify,
    description_subset,
    lakhs_per_annum,
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

__all__ = ["API_BASE", "API_VERSION", "NaukriAdapter", "NaukriConfig", "job_body"]

API_BASE = "https://api.naukri.com/rms/v1"
API_VERSION = "rms-v1 (partner documentation, 2026-10)"
PAGE_SIZE = 50
CV_MAX_BYTES = 5 * 1024 * 1024
ALLOWED_TAGS = frozenset({"p", "ul", "li", "b", "i", "br"})
_STATUS = {"active": "live", "live": "live", "expired": "expired", "closed": "closed", "rejected": "rejected", "pending": "pending", "under_review": "pending"}
FIELDS = FieldSchemaDTO(
    fields=(
        BoardFieldDTO("functional_area", "Functional area", True, "select"),
        BoardFieldDTO("industry", "Industry", True, "select"),
        BoardFieldDTO("role_category", "Role category", False, "text"),
        BoardFieldDTO("education", "Education", False, "text"),
    )
)


class NaukriConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str = ""
    default_functional_area: str | None = None
    default_industry: str | None = None
    response_poll: bool = True


def _years(value: Decimal | None) -> int | None:
    return int(value) if value is not None else None


def job_body(posting: JobPostingDTO, config: NaukriConfig) -> dict[str, Any]:
    fields = dict(posting.board_fields)
    body: dict[str, Any] = {
        "referenceId": str(posting.posting_id),
        "title": posting.title[:150],
        "jobDescription": description_subset(posting.description_html, ALLOWED_TAGS),
        "functionalArea": fields.get("functional_area") or config.default_functional_area,
        "industry": fields.get("industry") or config.default_industry,
        "roleCategory": fields.get("role_category"),
        "locations": [fields["location"]] if fields.get("location") else ([posting.location.city] if posting.location and posting.location.city else []),
        "minExp": _years(posting.experience_min_years),
        "maxExp": _years(posting.experience_max_years),
        "vacancies": max(posting.openings, 1),
        "keySkills": list(posting.skills[:50]),
        "education": fields.get("education"),
        "workMode": posting.work_mode,
        "applyRedirectUrl": posting.apply_url,
        "expiryDate": posting.expires_at.date().isoformat() if posting.expires_at else None,
    }
    if posting.salary is not None:
        lo = lakhs_per_annum(posting.salary.min_amount, posting.salary.period)
        hi = lakhs_per_annum(posting.salary.max_amount, posting.salary.period)
        if lo is not None or hi is not None:
            body["ctc"] = {"currency": posting.salary.currency, "minLakhs": str(lo) if lo is not None else None, "maxLakhs": str(hi) if hi is not None else None}
    if posting.company_name:
        body["companyName"] = posting.company_name
    return {k: v for k, v in body.items() if v is not None}


class NaukriAdapter(LiveJobBoardBase):
    provider_key = "naukri"

    @property
    def config(self) -> NaukriConfig:
        cfg = self.ctx.config
        return cfg if isinstance(cfg, NaukriConfig) else NaukriConfig.model_validate(cfg.model_dump())

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.secret("api_key"),
            "x-api-secret": self.secret("api_secret"),
            "x-account-id": self.config.account_id,
            "Accept": "application/json",
        }

    def _ref(self, data: Mapping[str, Any], fallback: str | None = None) -> BoardRefDTO:
        ref = str(data.get("jobId") or fallback or "")
        if not ref:
            raise ConnectorPermanentError("board_validation", "The board returned no job id")
        status = _STATUS.get(str(data.get("status") or "active").lower(), "pending")
        credits = data.get("creditsLeft")
        return BoardRefDTO(
            board_ref=ref,
            url=data.get("jobUrl") or None,
            status=status,  # type: ignore[arg-type]
            message=sanitise_message(data.get("message")) or None,
            expires_at=parse_dt(data.get("expiryDate")),
            views=int(data["views"]) if data.get("views") is not None else None,
            applies=int(data["responses"]) if data.get("responses") is not None else None,
            credits_left=int(credits) if credits is not None else None,
        )

    async def health(self) -> None:
        resp = await self.http.get(f"{API_BASE}/account", headers=self._headers())
        classify(resp, "account")

    async def publish(self, posting: JobPostingDTO) -> BoardRefDTO:
        resp = await self.http.post(f"{API_BASE}/jobs", headers=self._headers(), json=job_body(posting, self.config))
        classify(resp, "publish")
        return self._ref(resp.json())

    async def update(self, board_ref: str, posting: JobPostingDTO) -> BoardRefDTO:
        resp = await self.http.request("PUT", f"{API_BASE}/jobs/{board_ref}", headers=self._headers(), json=job_body(posting, self.config))
        classify(resp, "update")
        return self._ref(resp.json(), board_ref)

    async def close(self, board_ref: str) -> BoardRefDTO:
        resp = await self.http.post(f"{API_BASE}/jobs/{board_ref}/expire", headers=self._headers(), json={})
        classify(resp, "close")
        data = dict(resp.json() or {})
        data.setdefault("status", "closed")
        return self._ref(data, board_ref)

    async def status(self, board_ref: str) -> BoardRefDTO:
        resp = await self.http.get(f"{API_BASE}/jobs/{board_ref}", headers=self._headers())
        classify(resp, "status")
        return self._ref(resp.json(), board_ref)

    async def _cv(self, url: str | None) -> bytes | None:
        if not url:
            return None
        try:
            async with self.sibling_client(CV_MAX_BYTES) as client:
                resp = await client.get(url, headers=self._headers())
        except EgressRefused:
            return None
        return resp.content if resp.is_success and resp.content else None

    async def fetch_applicants(
        self, since: datetime | None, cursor: str | None
    ) -> tuple[Sequence[BoardApplicantDTO], str | None]:
        params: dict[str, Any] = {"page": int(cursor) if cursor and cursor.isdigit() else 1, "pageSize": PAGE_SIZE}
        if since is not None:
            params["since"] = since.isoformat()
        resp = await self.http.get(f"{API_BASE}/responses", headers=self._headers(), params=params)
        classify(resp, "responses")
        data = resp.json() or {}
        out: list[BoardApplicantDTO] = []
        for item in data.get("responses") or []:
            cv = await self._cv(item.get("cvUrl"))
            try:
                exp = Decimal(str(item["totalExperience"])) if item.get("totalExperience") is not None else None
            except InvalidOperation:
                exp = None
            out.append(
                BoardApplicantDTO(
                    board_application_id=str(item.get("responseId")),
                    board_ref=str(item.get("jobId")),
                    applied_at=parse_dt(item.get("appliedOn")) or self.ctx.now(),
                    full_name=str(item.get("name") or "").strip()[:200] or "Naukri applicant",
                    email=item.get("email") or None,
                    phone_e164=item.get("mobile") or None,
                    resume=cv,
                    resume_filename=item.get("cvFileName") if cv else None,
                    resume_media_type=None,
                    answers=tuple((str(a.get("question") or "")[:300], str(a.get("answer") or "")[:2000]) for a in item.get("answers") or []),
                    location=item.get("currentLocation") or None,
                    current_title=item.get("currentDesignation") or None,
                    experience_years=exp,
                )
            )
        nxt = data.get("nextPage")
        return out, (str(nxt) if nxt else None)

    async def field_schema(self) -> FieldSchemaDTO:
        return FIELDS
