# ruff: file-ignore[line-too-long]
"""Connector Manifests of the job boards (Epic 70, spec A2): ``naukri``, ``linkedin``, ``indeed`` and ``job_feed``.
Registered by ``app.services.job_distribution.hooks`` (``register_connector``); Epic 69 syncs the listings.

Every board has the foundation's ``fake_job_board`` (``fake``) and ``manual`` adapters; the real adapters (``live``)
run only with vault credentials and configuration (``mode='live'``), D-3 / D-13. The feed has no network at all.
"""

from app.core.integrations import (
    ConnectorManifest,
    DataFlow,
    OAuthProviderSpec,
    SecretField,
    SyncJobSpec,
    WebhookSpec,
)
from app.services.connectors.job_board.fake import FakeJobBoard
from app.services.connectors.job_board.feed import JobFeedAdapter
from app.services.connectors.job_board.indeed import IndeedAdapter, IndeedConfig
from app.services.connectors.job_board.linkedin import (
    AUTHORIZE_URL,
    TOKEN_URL,
    LinkedInAdapter,
    LinkedInConfig,
)
from app.services.connectors.job_board.manual import ManualJobBoard
from app.services.connectors.job_board.naukri import NaukriAdapter, NaukriConfig

__all__ = ["INDEED", "JOB_BOARD_MANIFESTS", "JOB_FEED", "LINKEDIN", "NAUKRI"]

PUBLISH, APPLICANTS, STATUS, FIELDS = "job_board.publish", "job_board.applicants", "job_board.status", "job_board.fields"
CLOSE_EVENTS = ("posting.closed", "demand.status_changed")
OUT_FIELDS = ("title", "description", "location", "experience", "salary", "company", "apply_url")
IN_FIELDS = ("name", "email", "phone", "resume", "answers", "location", "current_title", "experience")


def _out(board: str, country: str = "IN") -> DataFlow:
    return DataFlow("out", "job_posting", OUT_FIELDS, f"Advertise your open job on {board}", country, False)


def _in(board: str) -> DataFlow:
    return DataFlow("in", "applicant", IN_FIELDS, f"Receive applications for your jobs from {board}", "IN", True)


def _push_jobs(*, with_status: bool = True) -> tuple[SyncJobSpec, ...]:
    jobs = [
        SyncJobSpec(key="publish", capability=PUBLISH, direction="push", trigger="manual", entity_type="board_posting"),
        SyncJobSpec(key="update", capability=PUBLISH, direction="push", trigger="manual", entity_type="board_posting"),
        SyncJobSpec(key="close", capability=PUBLISH, direction="push", trigger="event", event_types=CLOSE_EVENTS, entity_type="board_posting"),
        SyncJobSpec(key="mark_out_of_date", capability=PUBLISH, direction="push", trigger="event", event_types=("posting.published",), entity_type="board_posting"),
    ]
    if with_status:
        jobs.append(SyncJobSpec(key="status", capability=STATUS, direction="pull", trigger="schedule", every_seconds=3600, entity_type="board_posting"))
    return tuple(jobs)


NAUKRI = ConnectorManifest(
    key="naukri", version=1, owner_epic="70", name="Naukri RMS",
    summary="Post jobs to Naukri.com, sync their status, expiry and credits, and receive Naukri responses as applications.",
    category="job_board", capabilities=frozenset({PUBLISH, APPLICANTS, STATUS, FIELDS}), auth="api_key",
    config_model=NaukriConfig,
    secret_fields=(SecretField("api_key", "API key", "api_key"), SecretField("api_secret", "API secret", "password")),
    data_flows=(_out("Naukri"), _in("Naukri")),
    egress_hosts=("api.naukri.com", "*.naukri.com"),
    sync_jobs=(
        *_push_jobs(),
        SyncJobSpec(key="applicants", capability=APPLICANTS, direction="pull", trigger="schedule", every_seconds=900, entity_type="board_application"),
    ),
    adapters={"fake": FakeJobBoard, "manual": ManualJobBoard, "live": NaukriAdapter},
    docs_url="https://www.naukri.com/recruiter", rate_per_minute=30,
)  # fmt: skip

LINKEDIN = ConnectorManifest(
    key="linkedin", version=1, owner_epic="70", name="LinkedIn Jobs",
    summary="Post jobs to LinkedIn from your company page. Candidates apply on your careers site (posting only).",
    category="job_board", capabilities=frozenset({PUBLISH, STATUS, FIELDS}), auth="oauth2_code",
    oauth=OAuthProviderSpec(authorize_url=AUTHORIZE_URL, token_url=TOKEN_URL, scopes=("r_organization_admin", "w_organization_social")),
    config_model=LinkedInConfig,
    secret_fields=(SecretField("oauth_refresh", "LinkedIn connection", "oauth_refresh"),),
    data_flows=(_out("LinkedIn", "US"),),
    egress_hosts=("api.linkedin.com", "www.linkedin.com"),
    sync_jobs=_push_jobs(),
    adapters={"fake": FakeJobBoard, "manual": ManualJobBoard, "live": LinkedInAdapter},
    docs_url="https://learn.microsoft.com/linkedin/talent/job-postings", rate_per_minute=30,
)  # fmt: skip

INDEED = ConnectorManifest(
    key="indeed", version=1, owner_epic="70", name="Indeed",
    summary="Post jobs to Indeed (Job Sync API) and receive Indeed Apply applications.",
    category="job_board", capabilities=frozenset({PUBLISH, APPLICANTS, STATUS, FIELDS}), auth="oauth2_client",
    config_model=IndeedConfig,
    secret_fields=(
        SecretField("client_id", "Client ID", "api_key"),
        SecretField("client_secret", "Client secret", "password"),
        SecretField("webhook_secret", "Indeed Apply API secret", "webhook_secret"),
    ),
    data_flows=(_out("Indeed", "US"), _in("Indeed")),
    egress_hosts=("apis.indeed.com", "secure.indeed.com"),
    webhook=WebhookSpec(scheme="custom", signature_header="X-Indeed-Signature", event_id_path="/id"),
    sync_jobs=_push_jobs(),
    adapters={"fake": FakeJobBoard, "manual": ManualJobBoard, "live": IndeedAdapter},
    docs_url="https://docs.indeed.com/job-sync-api", rate_per_minute=30,
)  # fmt: skip

JOB_FEED = ConnectorManifest(
    key="job_feed", version=1, owner_epic="70", name="XML job feed",
    summary="Publish your live jobs as an XML feed that aggregators and job sites can read.",
    category="job_board", capabilities=frozenset({PUBLISH, STATUS}), auth="none",
    data_flows=(DataFlow("out", "job_posting", OUT_FIELDS, "Publish your open jobs in a public XML feed", "IN", False),),
    sync_jobs=_push_jobs(with_status=False),
    adapters={"fake": JobFeedAdapter},
    sandbox_safe=True,
)  # fmt: skip

JOB_BOARD_MANIFESTS = (NAUKRI, LINKEDIN, INDEED, JOB_FEED)
