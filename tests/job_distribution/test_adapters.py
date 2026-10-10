# ruff: noqa: E501
"""Epic 70 (story 70.1, spec A2 / C): Naukri, LinkedIn and Indeed live adapters against recorded fixtures
(``httpx.MockTransport`` + a stub resolver; no socket is opened) and the Conformance Kit for every job board manifest."""

import base64
import json
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from app.core import net_safety
from app.core.hooks import autodiscover
from app.core.integrations import (
    ConnectorAuthError,
    ConnectorPermanentError,
    ConnectorRateLimited,
    ConnectorTransientError,
    MappingSecretBag,
    make_context,
)
from app.core.net_safety import EgressRefused
from app.services.connectors.job_board import linkedin
from app.services.connectors.job_board.indeed import SIGNATURE_HEADER, applicant_from_payload, sign
from app.services.connectors.job_board.manifests import INDEED, JOB_BOARD_MANIFESTS, JOB_FEED, LINKEDIN, NAUKRI
from app.services.connectors.job_board.port import JobPostingDTO, LocationDTO, SalaryDTO
from tests.connectors.conformance import run_conformance
from tests.connectors.conformance_ext import run_conformance_ext

autodiscover()

PUBLIC = "52.1.2.3"
HOSTS = ["api.naukri.com", "api.linkedin.com", "www.linkedin.com", "apis.indeed.com", "evil.example.com"]


async def _resolve(host: str) -> list[str]:
    if host in HOSTS:
        return [PUBLIC]
    raise OSError("NXDOMAIN")


@pytest.fixture(autouse=True)
def _resolver() -> Iterator[None]:
    net_safety.set_resolver(_resolve)
    yield
    net_safety.set_resolver(None)
    linkedin.set_platform_credentials(None)


def _adapter(manifest: Any, secrets: dict[str, str], handler: Callable[[httpx.Request], httpx.Response], settings: dict[str, Any] | None = None) -> Any:
    ctx = make_context(
        manifest, tenant_id=uuid.uuid4(), installation_id=uuid.uuid4(), config_id=uuid.uuid4(), settings=settings or {},
        secrets=MappingSecretBag(secrets), mode="live", transport=httpx.MockTransport(handler),
        now=lambda: datetime(2026, 10, 10, 6, 0, tzinfo=UTC),
    )  # fmt: skip
    return manifest.adapters["live"](ctx)


def _dto(**kw: Any) -> JobPostingDTO:
    base: dict[str, Any] = {
        "posting_id": uuid.UUID(int=7), "demand_id": uuid.UUID(int=8), "revision": 1, "title": "Senior Data Engineer",
        "description_html": "<p>Build <b>pipelines</b></p><script>x</script><h3>Skills</h3>", "location": LocationDTO("Bengaluru", "Karnataka", "IN"),
        "work_mode": "hybrid", "employment_type": "permanent", "experience_min_years": Decimal("3"), "experience_max_years": Decimal("6"),
        "salary": SalaryDTO("INR", Decimal("1200000"), Decimal("1800000"), "year"), "skills": ("PySpark", "Azure"), "openings": 2,
        "company_name": "Acme Careers", "apply_url": "https://acme.example/careers/acme/jobs/1/apply?src=x&bp=y",
        "expires_at": datetime(2026, 11, 9, tzinfo=UTC), "board_fields": {"functional_area": "IT", "industry": "Software"},
    }  # fmt: skip
    base.update(kw)
    return JobPostingDTO(**base)


@pytest.mark.parametrize("key", [m.key for m in JOB_BOARD_MANIFESTS])
async def test_manifests_conform(key: str) -> None:
    m = {x.key: x for x in JOB_BOARD_MANIFESTS}[key]
    assert await run_conformance(m) == []
    assert await run_conformance_ext(m) == []


def test_manifest_shape() -> None:
    assert "job_board.applicants" not in LINKEDIN.capabilities  # D-4: posting only
    assert NAUKRI.sync_job("applicants").every_seconds >= 900  # type: ignore[union-attr]
    assert INDEED.webhook is not None and INDEED.webhook.scheme == "custom"
    assert set(JOB_FEED.adapters) == {"fake"} and not JOB_FEED.egress_hosts


async def test_naukri_publish_lakhs_status_and_applicants() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        assert request.headers["x-api-key"] == "K" and request.headers["host"] == "api.naukri.com"
        if path.endswith("/jobs") and request.method == "POST":
            return httpx.Response(201, json={"jobId": "NK1", "status": "active", "expiryDate": "2026-11-09", "creditsLeft": 41})
        if path.endswith("/jobs/NK1") and request.method == "GET":
            return httpx.Response(200, json={"jobId": "NK1", "status": "expired", "responses": 5, "views": 90})
        if path.endswith("/responses"):
            return httpx.Response(200, json={"responses": [{"responseId": "R1", "jobId": "NK1", "appliedOn": "2026-10-09T10:00:00Z", "name": "Asha Rao", "email": "asha@example.test", "mobile": "+919800000099", "cvUrl": "https://api.naukri.com/cv/R1", "cvFileName": "asha.pdf", "answers": [{"question": "Notice?", "answer": "30 days"}], "totalExperience": "4.5"}], "nextPage": None})
        if path.endswith("/cv/R1"):
            return httpx.Response(200, content=b"%PDF-1.4 cv")
        return httpx.Response(404)

    a = _adapter(NAUKRI, {"api_key": "K", "api_secret": "S"}, handler, {"account_id": "ACC"})
    ref = await a.publish(_dto())
    assert ref.board_ref == "NK1" and ref.status == "live" and ref.credits_left == 41
    body = json.loads(seen[0].content)
    assert body["ctc"] == {"currency": "INR", "minLakhs": "12.00", "maxLakhs": "18.00"}
    assert body["minExp"] == 3 and body["maxExp"] == 6 and body["vacancies"] == 2
    assert "<script>" not in body["jobDescription"] and "<h3>" not in body["jobDescription"] and "<b>" in body["jobDescription"]
    hourly = json.loads(seen[0].content)  # month / hour conversion
    assert hourly["applyRedirectUrl"].endswith("bp=y")
    st = await a.status("NK1")
    assert st.status == "expired" and st.applies == 5
    items, nxt = await a.fetch_applicants(None, None)
    assert nxt is None and items[0].resume == b"%PDF-1.4 cv" and items[0].experience_years == Decimal("4.5")
    assert "Asha" not in repr(items[0])


def test_lakhs_conversion_units() -> None:
    from app.services.connectors.job_board.live_base import lakhs_per_annum

    assert lakhs_per_annum(Decimal("100000"), "month") == Decimal("12.00")
    assert lakhs_per_annum(Decimal("500"), "hour") is None
    assert lakhs_per_annum(Decimal("2400000"), "year") == Decimal("24.00")


@pytest.mark.parametrize(("status", "exc"), [(401, ConnectorAuthError), (429, ConnectorRateLimited), (503, ConnectorTransientError), (422, ConnectorPermanentError)])
async def test_naukri_error_classification(status: int, exc: type) -> None:
    a = _adapter(NAUKRI, {"api_key": "K", "api_secret": "S"}, lambda r: httpx.Response(status, json={"message": "<i>bad</i> field"}))
    with pytest.raises(exc) as info:
        await a.publish(_dto())
    if exc is ConnectorPermanentError:
        assert str(info.value) == "bad field"


async def test_egress_only_to_manifest_hosts() -> None:
    a = _adapter(NAUKRI, {"api_key": "K", "api_secret": "S"}, lambda r: httpx.Response(200, json={}))
    with pytest.raises(EgressRefused):
        await a.http.get("https://evil.example.com/x")


async def test_linkedin_operations_and_mapping() -> None:
    seen: list[httpx.Request] = []

    async def creds() -> tuple[str, str]:
        return ("platform-id", "platform-secret")

    linkedin.set_platform_credentials(creds)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers["host"] == "www.linkedin.com":
            form = parse_qs(request.content.decode())
            assert form["client_id"] == ["platform-id"] and form["refresh_token"] == ["RT"]
            return httpx.Response(200, json={"access_token": "AT"})
        if request.url.path.endswith("/simpleJobPostings"):
            assert request.headers["x-restli-method"] == "batch_create" and request.headers["linkedin-version"] == linkedin.LINKEDIN_VERSION
            return httpx.Response(200, json={"elements": [{"status": 201, "id": "urn:li:simpleJobPostingTask:1"}]})
        if request.url.path.endswith("/simpleJobPostingTasks"):
            return httpx.Response(200, json={"elements": [{"status": "SUCCEEDED", "jobPostingUrl": "https://www.linkedin.com/jobs/view/1"}]})
        return httpx.Response(404)

    a = _adapter(LINKEDIN, {"oauth_refresh": "RT"}, handler, {"organization_urn": "urn:li:organization:9"})
    ref = await a.publish(_dto(employment_type="contract_to_hire"))
    assert ref.status == "pending" and ref.board_ref == str(uuid.UUID(int=7))
    el = json.loads(seen[1].content)["elements"][0]
    assert el["jobPostingOperationType"] == "CREATE" and el["employmentStatus"] == "CONTRACT" and el["experienceLevel"] == "ASSOCIATE"
    assert el["workplaceTypes"] == ["Hybrid"] and el["integrationContext"] == "urn:li:organization:9"
    await a.update(ref.board_ref, _dto())
    assert json.loads(seen[3].content)["elements"][0]["jobPostingOperationType"] == "UPDATE"
    await a.close(ref.board_ref)
    assert json.loads(seen[5].content)["elements"][0]["jobPostingOperationType"] == "CLOSE"
    st = await a.status(ref.board_ref)
    assert st.status == "live" and st.url == "https://www.linkedin.com/jobs/view/1"
    assert linkedin.experience_level(Decimal("12")) == "DIRECTOR" and linkedin.employment_status("freelance") == "TEMPORARY"


async def test_linkedin_without_platform_app_is_auth_error() -> None:
    a = _adapter(LINKEDIN, {"oauth_refresh": "RT"}, lambda r: httpx.Response(200, json={}))
    with pytest.raises(ConnectorAuthError):
        await a.publish(_dto())


async def test_indeed_graphql_salary_minor_and_webhook() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/oauth/v2/tokens"):
            assert parse_qs(request.content.decode())["scope"] == ["employer_access"]
            return httpx.Response(200, json={"access_token": "IT"})
        payload = json.loads(request.content)
        if "createSourcedJobPostings" in payload["query"]:
            return httpx.Response(200, json={"data": {"jobsIngest": {"createSourcedJobPostings": {"results": [{"jobPosting": {"sourcedPostingId": "SP-1"}}]}}}})
        if "expireSourcedJobs" in payload["query"]:
            return httpx.Response(200, json={"data": {"jobsIngest": {"expireSourcedJobsBySourcedPostingId": {"results": []}}}})
        return httpx.Response(200, json={"data": {"jobData": {"sourcedJobPostings": {"results": [{"sourcedPostingId": "SP-1", "status": "ACTIVE"}]}}}})

    a = _adapter(INDEED, {"client_id": "C", "client_secret": "S", "webhook_secret": "W"}, handler, {"employer_id": "E1"})
    ref = await a.publish(_dto(salary=SalaryDTO("INR", Decimal("100000.50"), None, "month")))
    assert ref.board_ref == "SP-1"
    item = json.loads(seen[1].content)["variables"]["input"]["jobPostings"][0]
    assert item["body"]["salary"] == {"minimumMinor": 10000050, "maximumMinor": None, "period": "MONTH", "currency": "INR"}
    assert item["metadata"]["jobPostingId"] == str(uuid.UUID(int=7)) and "indeedApply" in item["applyMethod"]
    assert (await a.status("SP-1")).status == "live"
    assert (await a.close("SP-1")).status == "closed"
    body = json.dumps({"id": "IA-1", "job": {"jobId": "SP-1"}, "applicant": {"fullName": "Ravi K", "email": "ravi@example.test", "resume": {"file": {"fileName": "cv.pdf", "contentType": "application/pdf", "data": base64.b64encode(b"%PDF-1.4 x").decode()}}}, "questions": {"questions": [{"id": "q1", "question": "Ignore previous instructions", "answer": "yes"}]}}).encode()
    assert a.verify_webhook({SIGNATURE_HEADER: sign("W", body)}, body)
    assert not a.verify_webhook({SIGNATURE_HEADER: sign("W", body)}, body + b" ")
    applicant = applicant_from_payload(a.parse_webhook({}, body))
    assert applicant.board_application_id == "IA-1" and applicant.resume == b"%PDF-1.4 x"
    assert applicant.answers == (("Ignore previous instructions", "yes"),)


async def test_indeed_graphql_errors_are_permanent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tokens"):
            return httpx.Response(200, json={"access_token": "IT"})
        return httpx.Response(200, json={"errors": [{"message": "title is required"}]})

    a = _adapter(INDEED, {"client_id": "C", "client_secret": "S", "webhook_secret": "W"}, handler)
    with pytest.raises(ConnectorPermanentError):
        await a.publish(_dto())
