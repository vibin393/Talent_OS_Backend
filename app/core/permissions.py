"""Authorization primitives (AD-11): permission catalog, scopes, the resolved ``Principal``, ``require()``,
``scope_filter()`` and compensation masking.

* Permissions are ``resource:action`` strings, defined **once** here (``CATALOG``) for all of Phase 1.
* A role maps permission -> scope. Scopes are ordered ``own < team < bu < tenant``; a user holding several
  roles gets, per permission, the widest scope of any of their roles.
* Routers declare ``Depends(require("candidate:read"))``; services narrow queries with ``scope_filter``.
* Compensation fields are masked (``mask_compensation``) for principals without ``compensation:view``.

Core never imports modules (AD-1): the authentication dependency that builds the ``Principal`` lives in
``identity`` and registers itself via ``set_principal_resolver``; ``scope_filter`` reaches the
``team_members`` / ``users`` tables through lightweight ``sqlalchemy.table()`` constructs.
"""

import dataclasses
import enum
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import Depends, Request
from pydantic import BaseModel
from sqlalchemy import ColumnElement, Select, column, false, or_, select, table
from sqlalchemy.orm import QueryableAttribute

from app.core.errors import Forbidden

# A mapped attribute (``User.id``) or any SQL column expression.
Column_ = ColumnElement[Any] | QueryableAttribute[Any]

PrincipalType = Literal[
    "user", "candidate", "platform", "support", "api_key", "client_user", "vendor_user", "agent", "app"
]
# Phase 2 external principals (AD-26): they never pass ``require()`` (internal routes only).
EXTERNAL_PRINCIPAL_TYPES: frozenset[str] = frozenset({"client_user", "vendor_user"})
# Epic 53 (AD-60): agent principals act only through tools inside the runtime, never through a router.
NEVER_ROUTED_PRINCIPAL_TYPES: frozenset[str] = EXTERNAL_PRINCIPAL_TYPES | {"agent"}


# ================================================================ scopes
class Scope(enum.StrEnum):
    """Row visibility of a granted permission, narrowest first."""

    OWN = "own"
    TEAM = "team"
    BU = "bu"
    TENANT = "tenant"

    @property
    def rank(self) -> int:
        return _SCOPE_ORDER.index(self)

    def covers(self, other: "Scope") -> bool:
        return self.rank >= other.rank


_SCOPE_ORDER: tuple[Scope, ...] = (Scope.OWN, Scope.TEAM, Scope.BU, Scope.TENANT)
ALL_SCOPES: tuple[Scope, ...] = _SCOPE_ORDER
TENANT_ONLY: tuple[Scope, ...] = (Scope.TENANT,)


def widest(a: Scope, b: Scope) -> Scope:
    return a if a.rank >= b.rank else b


# ================================================================ catalog
@dataclasses.dataclass(frozen=True, slots=True)
class PermissionDef:
    key: str
    group: str
    description: str
    # Scopes a role may grant this permission at. Administrative permissions are tenant-wide only.
    scopes: tuple[Scope, ...] = ALL_SCOPES
    # Epic 14 (FR-81): sensitive permissions are never added to custom Roles automatically (resyncs, "select
    # all" in the UI); only the Tenant Admin and the Phase 2 default Roles that need them hold them.
    sensitive: bool = False


def _crud(resource: str, group: str, label: str, *extra: tuple[str, str]) -> list[PermissionDef]:
    perms = [
        PermissionDef(f"{resource}:read", group, f"View {label}"),
        PermissionDef(f"{resource}:create", group, f"Create {label}"),
        PermissionDef(f"{resource}:update", group, f"Edit {label}"),
        PermissionDef(f"{resource}:delete", group, f"Delete or archive {label}"),
    ]
    perms.extend(PermissionDef(f"{resource}:{action}", group, desc) for action, desc in extra)
    return perms


def _admin(resource: str, group: str, label: str) -> list[PermissionDef]:
    return [
        PermissionDef(f"{resource}:read", group, f"View {label}", TENANT_ONLY),
        PermissionDef(f"{resource}:manage", group, f"Manage {label}", TENANT_ONLY),
    ]


_PERMISSIONS: list[PermissionDef] = [
    *_crud("client", "Clients", "clients"),
    *_crud("contact", "Clients", "client contacts"),
    *_crud("demand", "Demands", "demands", ("assign", "Assign recruiters to demands")),
    PermissionDef("demand:approve", "Demands", "Approve or reject demands (demand approvals)"),
    *_crud("candidate", "Candidates", "candidates", ("merge", "Merge duplicate candidates")),
    *_crud("application", "Pipeline", "applications", ("move", "Move applications between stages")),
    PermissionDef("submission:read", "Pipeline", "View client submissions"),
    PermissionDef("submission:create", "Pipeline", "Submit candidates to clients"),
    PermissionDef("submission:update", "Pipeline", "Record client feedback on submissions"),
    # Epic 6: human-only reject, screenings, tenant pipeline configuration.
    PermissionDef("application:reject", "Pipeline", "Reject applications (and confirm client rejections)"),
    PermissionDef("screening:write", "Pipeline", "Record screenings and send AI screenings"),
    PermissionDef(
        "pipeline:configure", "Pipeline", "Configure pipeline stages, reasons, questionnaires", TENANT_ONLY
    ),
    *_crud("interview", "Interviews", "interviews"),
    PermissionDef("scorecard:read", "Interviews", "View interview scorecards"),
    PermissionDef("scorecard:submit", "Interviews", "Submit interview scorecards"),
    # Epic 7: scheduling, kits / templates / settings, stage outcomes, own calendar + mailbox connections.
    PermissionDef("interview:schedule", "Interviews", "Plan interview stages and schedule interviews"),
    PermissionDef(
        "interview:configure",
        "Interviews",
        "Manage interview kits, stage templates and settings",
        TENANT_ONLY,
    ),
    PermissionDef("interview:outcome", "Interviews", "Record interview stage outcomes and select candidates"),
    PermissionDef(
        "connection:manage_own", "Interviews", "Connect own Google / Microsoft calendar and mailbox"
    ),
    PermissionDef("offer:read", "Offers", "View offers"),
    PermissionDef("offer:create", "Offers", "Create offers"),
    PermissionDef("offer:update", "Offers", "Edit offers"),
    PermissionDef("offer:approve", "Offers", "Approve offers"),
    # Epic 8: draft / revise, release to the candidate, withdraw, letter templates.
    PermissionDef("offer:write", "Offers", "Draft and revise offers"),
    PermissionDef("offer:release", "Offers", "Release approved offers to candidates"),
    PermissionDef("offer:withdraw", "Offers", "Withdraw offers"),
    PermissionDef(
        "offer_template:manage", "Offers", "Manage offer letter templates and offer settings", TENANT_ONLY
    ),
    *_crud("task", "Tasks", "tasks"),
    # Notes / logged calls and meetings on any timeline the principal can see (Epic 2).
    PermissionDef("note:write", "Activity", "Add notes and log calls or meetings", TENANT_ONLY),
    PermissionDef("activity:read_private", "Activity", "See other users' private notes", TENANT_ONLY),
    PermissionDef("report:read", "Reports", "View dashboards and reports"),
    PermissionDef("report:export", "Reports", "Export reports and lists"),
    *_admin("user", "Administration", "users and invitations"),
    *_admin("role", "Administration", "roles and permissions"),
    *_admin("team", "Administration", "teams"),
    *_admin("business_unit", "Administration", "business units"),
    *_admin("settings", "Administration", "tenant settings"),
    PermissionDef("audit:read", "Administration", "View the audit log", TENANT_ONLY),
    PermissionDef("ai:admin", "Administration", "Configure AI features and budgets", TENANT_ONLY),
    *_admin("privacy", "Privacy", "consents and data subject requests (DSR)"),
    PermissionDef("compensation:view", "Privacy", "See compensation, rate and margin fields", TENANT_ONLY),
    # Epic 12: privacy, AI governance, platform operations, API keys.
    PermissionDef("consent:read", "Privacy", "View candidate consents", TENANT_ONLY),
    PermissionDef("consent:write", "Privacy", "Request and record candidate consents", TENANT_ONLY),
    PermissionDef("dsr:read", "Privacy", "View data subject requests", TENANT_ONLY),
    PermissionDef("dsr:process", "Privacy", "Process data subject requests (incl. erasure)", TENANT_ONLY),
    PermissionDef("retention:manage", "Privacy", "Configure and run the retention policy", TENANT_ONLY),
    PermissionDef("ai_log:read", "Administration", "View and export the AI action log", TENANT_ONLY),
    PermissionDef("support_access:approve", "Administration", "Approve vendor support access", TENANT_ONLY),
    PermissionDef("api_key:manage", "Administration", "Create, rotate and revoke API keys", TENANT_ONLY),
    # Epic 10: email, templates and workflow rules (tasks use the task:* keys above; notifications are always
    # the caller's own).
    PermissionDef("email:send", "Communication", "Email candidates and contacts from Talent OS"),
    PermissionDef("email_template:read", "Communication", "View and use email templates"),
    PermissionDef("email_template:manage", "Communication", "Manage email templates", TENANT_ONLY),
    PermissionDef("workflow:read", "Administration", "View workflow rules and executions", TENANT_ONLY),
    PermissionDef("workflow:manage", "Administration", "Manage and test workflow rules", TENANT_ONLY),
    # Epic 5: AI matching (match rows are scoped through the demand: ``demand:read`` is checked as well).
    PermissionDef("match:read", "Matching", "View AI matches and the rediscovery funnel", TENANT_ONLY),
    PermissionDef(
        "match:write",
        "Matching",
        "Dismiss, restore and recompute matches; add matches to the pipeline",
        TENANT_ONLY,
    ),
    PermissionDef(
        "matching:configure", "Matching", "Configure matching weights, thresholds and proxies", TENANT_ONLY
    ),
    # Epic 9: career site branding and apply settings (``dsr:read`` above serves the portal DSR intake list).
    PermissionDef("career_site:configure", "Administration", "Configure the career site", TENANT_ONLY),
    # Epic 13 (Phase 2 foundation): legal entities, numbering, FX, periods, identity data, connectors, offline
    # approvals. The Tenant Admin holds them dynamically (``effective_role_grants``); no other default role.
    PermissionDef(
        "legal_entity:read", "Finance", "View legal entities, GST registrations and periods", TENANT_ONLY
    ),
    PermissionDef(
        "legal_entity:write",
        "Finance",
        "Manage legal entities, GST registrations and bank accounts",
        TENANT_ONLY,
    ),
    PermissionDef("document_series:write", "Finance", "Manage invoice / note numbering series", TENANT_ONLY),
    PermissionDef("fx_rate:write", "Finance", "Record foreign exchange rates", TENANT_ONLY),
    PermissionDef("period:close", "Finance", "Close accounting periods (sensitive)", TENANT_ONLY),
    PermissionDef(
        "identity:view", "Privacy", "Unmask bank account, PAN and ID numbers (sensitive)", TENANT_ONLY
    ),
    PermissionDef("connector:configure", "Administration", "Configure provider connectors", TENANT_ONLY),
    PermissionDef(
        "approval:record_offline",
        "Administration",
        "Record an approval given offline (with evidence)",
        TENANT_ONLY,
    ),
    # Epic 32: SLA targets, clocks and escalation policies (workflow rules stay under workflow:*). Tenant Admin only.
    PermissionDef("sla:read", "Administration", "View SLA targets, clocks and escalations", TENANT_ONLY),
    PermissionDef("sla:manage", "Administration", "Manage SLA targets and escalation policies", TENANT_ONLY),
]

# Epic 14 (Phase 2 access; FR-77, FR-78, FR-79, FR-81): SSO, external-user access and the sensitive finance /
# BGV permissions. Owned here; domain epics reference these keys and never re-declare them.
_PERMISSIONS += [
    PermissionDef(
        "sso:manage", "Administration", "Configure single sign-on and identity providers", TENANT_ONLY
    ),
    PermissionDef(
        "client_portal:manage", "Clients", "Grant, scope and revoke Client Portal access", TENANT_ONLY
    ),
    PermissionDef(
        "vendor_portal:manage", "Vendors", "Invite and deactivate Vendor Portal users", TENANT_ONLY
    ),
    PermissionDef(
        "invoice:issue", "Finance", "Issue invoices and notes (sensitive)", TENANT_ONLY, sensitive=True
    ),
    PermissionDef(
        "payment:record", "Finance", "Record client payments (sensitive)", TENANT_ONLY, sensitive=True
    ),
    PermissionDef(
        "invoice:write_off", "Finance", "Write off invoice balances (sensitive)", TENANT_ONLY, sensitive=True
    ),
    PermissionDef(
        "bgv:adjudicate",
        "Compliance",
        "Adjudicate background verification results (sensitive)",
        TENANT_ONLY,
        sensitive=True,
    ),
    PermissionDef("executive_dashboard:read", "Reports", "View the executive dashboard", TENANT_ONLY),
]

# Epic 22 (documents & eSignature, FR-117..121). Catalogue only: the Tenant Admin holds them dynamically; the
# Phase 2 default roles (Epic 14) and the phase-close resync grant them to other roles. ``document:read|write``
# are additionally gated by the owning record's access (entity-access scope).
_PERMISSIONS += [
    PermissionDef(
        "document_template:manage", "Documents", "Manage document templates and eSign providers", TENANT_ONLY
    ),
    PermissionDef("document:read", "Documents", "View documents of records you can see"),
    PermissionDef(
        "document:write", "Documents", "Upload, replace and archive documents of records you can edit"
    ),
    PermissionDef("document:hr", "Documents", "Read and write HR-only documents", TENANT_ONLY),
    PermissionDef("document:finance", "Documents", "Read and write finance documents", TENANT_ONLY),
    PermissionDef("document:verify", "Documents", "Verify a signed document by its fingerprint", TENANT_ONLY),
    PermissionDef("signature_request:create", "Documents", "Send documents for electronic signature"),
    PermissionDef("signature_request:cancel", "Documents", "Cancel any signature request", TENANT_ONLY),
]
# Epic 16 (client commercials, FR-88..FR-92). Catalogue only: the Tenant Admin holds them dynamically; the
# Phase 2 default roles (Epic 14) and the phase-close resync grant them to other roles.
_PERMISSIONS += [
    PermissionDef("contract:read", "Commercials", "View client and vendor contracts", TENANT_ONLY),
    PermissionDef("contract:write", "Commercials", "Create, activate and terminate contracts", TENANT_ONLY),
    PermissionDef("rate_card:read", "Commercials", "View rate cards and rate checks", TENANT_ONLY),
    PermissionDef("rate_card:write", "Commercials", "Edit, import and publish rate cards", TENANT_ONLY),
    PermissionDef("po:read", "Commercials", "View purchase orders and their consumption", TENANT_ONLY),
    PermissionDef("po:write", "Commercials", "Create, edit, close and cancel purchase orders", TENANT_ONLY),
    PermissionDef("billing:read", "Finance", "View billing profiles and payment terms", TENANT_ONLY),
    PermissionDef("billing:write", "Finance", "Edit billing profiles and payment terms", TENANT_ONLY),
]
# Epic 33 (communication extensions, FR-171..FR-174). Catalogue only, like Epics 16 / 22: the Tenant Admin holds
# them dynamically; Recruiter, HR / Onboarding and Account Manager receive ``messaging:send`` at the phase-close
# resync (frozen P1 role snapshot, AD-16). Reply capture uses each user's own connection (no permission).
_PERMISSIONS += [
    PermissionDef("messaging:send", "Communication", "Send SMS and WhatsApp template messages"),
    PermissionDef(
        "messaging:configure",
        "Communication",
        "Configure SMS / WhatsApp templates, connectors and opt-outs",
        TENANT_ONLY,
    ),
    PermissionDef(
        "career_domain:manage", "Administration", "Manage the career site's custom domain", TENANT_ONLY
    ),
]
# Epic 19 (placements, pre-joining & workforce, FR-104..FR-109). Catalogue only, like Epics 16 / 22 / 33: the Tenant
# Admin holds them dynamically; at the phase-close resync HR / Onboarding receives read, join, drop, worker:read and
# worker_bank:approve, Account Manager read / create / update / rates, Recruiter read (frozen role snapshot, AD-16).
_PERMISSIONS += [
    PermissionDef("placement:read", "Workforce", "View placements, rate segments and start gates"),
    PermissionDef("placement:create", "Workforce", "Create placements directly"),
    PermissionDef("placement:update", "Workforce", "Edit non-commercial placement fields"),
    PermissionDef("placement:rates", "Workforce", "Add rate segments to placements"),
    PermissionDef("placement:join", "Workforce", "Mark a placement as joined", TENANT_ONLY),
    PermissionDef("placement:drop", "Workforce", "Drop a placement before joining", TENANT_ONLY),
    PermissionDef("placement:configure", "Workforce", "Configure placement settings", TENANT_ONLY),
    PermissionDef("start_gate:override", "Workforce", "Request a start-gate override", TENANT_ONLY),
    PermissionDef("worker:read", "Workforce", "View Worker 360 profiles", TENANT_ONLY),
    PermissionDef("worker_bank:approve", "Workforce", "Approve worker bank-detail changes", TENANT_ONLY),
]
# Epic 15 (sales CRM, FR-83..FR-87): leads and opportunities, data-scoped on ``owner_id`` (own|team|bu|tenant).
_PERMISSIONS += [
    PermissionDef("lead:read", "Sales", "View leads"),
    PermissionDef("lead:write", "Sales", "Create, edit, import, qualify, disqualify and convert leads"),
    PermissionDef("opportunity:read", "Sales", "View opportunities, the pipeline and the sales dashboard"),
    PermissionDef(
        "opportunity:write", "Sales", "Create and edit opportunities, move stages, confirm Won conversions"
    ),
]
# Epic 17 (vendors & vendor portal, FR-93..FR-99). Vendor statements use the ``payroll:*`` keys declared here
# (Epic 28 only references them). Default-role grants: see ``_EPIC17_ROLE_GRANTS`` below.
_PERMISSIONS += [
    PermissionDef(
        "vendor:read", "Vendors", "View vendors, their documents, releases and submissions", TENANT_ONLY
    ),
    PermissionDef(
        "vendor:write",
        "Vendors",
        "Create and edit vendors, change vendor status, vendor settings",
        TENANT_ONLY,
    ),
    PermissionDef(
        "vendor:release", "Vendors", "Release demands to vendors (pause, resume, withdraw)", TENANT_ONLY
    ),
    PermissionDef(
        "vendor_submission:review", "Vendors", "Review, accept and reject vendor submissions", TENANT_ONLY
    ),
    PermissionDef("vendor:performance", "Vendors", "View vendor performance scorecards", TENANT_ONLY),
    PermissionDef("payroll:read", "Finance", "View vendor payable statements and payroll runs", TENANT_ONLY),
    PermissionDef(
        "payroll:generate", "Finance", "Build vendor payable statements and payroll drafts", TENANT_ONLY
    ),
    PermissionDef(
        "payroll:finalize", "Finance", "Finalize vendor payable statements and payroll runs", TENANT_ONLY
    ),
]
# Epic 24 (bench management, FR-125..FR-127). Catalogue only, like Epic 19: the Tenant Admin holds them
# dynamically; at the phase-close resync Recruiter receives bench:read / bench:review_matches and Delivery Manager /
# Account Manager all four (frozen P1 role snapshot, AD-16).
_PERMISSIONS += [
    PermissionDef("bench:read", "Workforce", "View the bench, bench matches and the bench dashboard"),
    PermissionDef(
        "bench:write", "Workforce", "Add workers to the bench, edit and release bench entries", TENANT_ONLY
    ),
    PermissionDef("bench:edit_cost", "Workforce", "Override a bench worker's cost per day", TENANT_ONLY),
    PermissionDef("bench:review_matches", "Workforce", "Review or dismiss bench matches on demands"),
]
# Epic 21 (compliance & credentials, FR-110/115/116). Default-role grants: ``_EPIC21_ROLE_GRANTS`` below (Phase 2
# roles only; Account Manager receives ``compliance:read`` at the phase-close resync, frozen P1 snapshot AD-16).
_PERMISSIONS += [
    PermissionDef(
        "compliance:read", "Compliance", "View the compliance dashboard, checklists and credentials"
    ),
    PermissionDef(
        "compliance:configure",
        "Compliance",
        "Configure credential types, requirements and settings",
        TENANT_ONLY,
    ),
    PermissionDef(
        "compliance:waive", "Compliance", "Waive a placement compliance item (with a reason)", TENANT_ONLY
    ),
    PermissionDef("credential:write", "Compliance", "Record and renew worker credentials", TENANT_ONLY),
    PermissionDef(
        "credential:verify", "Compliance", "Verify credentials and review Aadhaar redactions", TENANT_ONLY
    ),
]
# Epic 23 (digital onboarding, FR-122..FR-124). ``onboarding:waive`` is the human-only waive (FR-181; enforced by
# the status machine). Default-role grants: ``_EPIC23_ROLE_GRANTS`` below (Phase 2 roles only; P1 Account Manager /
# Recruiter receive ``onboarding:read`` at the phase-close resync, AD-16).
_PERMISSIONS += [
    PermissionDef(
        "onboarding:read", "Workforce", "View onboarding cases, tasks and the onboarding board", TENANT_ONLY
    ),
    PermissionDef(
        "onboarding:manage",
        "Workforce",
        "Start onboarding, invite Workers, approve and return onboarding tasks",
        TENANT_ONLY,
    ),
    PermissionDef("onboarding:waive", "Workforce", "Waive onboarding tasks", TENANT_ONLY),
    PermissionDef(
        "onboarding:configure", "Workforce", "Configure onboarding templates and settings", TENANT_ONLY
    ),
]
# ``period:close`` and ``identity:view`` are declared by the foundation (Epic 13) and are sensitive too.
SENSITIVE_PERMISSIONS: frozenset[str] = frozenset(
    {p.key for p in _PERMISSIONS if p.sensitive} | {"period:close", "identity:view"}
)

# Epic 20 (BGV & reference checks, FR-110..FR-114). ``bgv:adjudicate`` is declared by Epic 14 above. Default-role
# grants: ``_EPIC20_ROLE_GRANTS`` below (Phase 2 roles only; the P1 Recruiter gets ``reference_check:request`` and
# ``bgv:read`` at the phase-close resync, AD-16).
_PERMISSIONS += [
    PermissionDef(
        "bgv:read", "Compliance", "View BGV cases, checks, discrepancies and outcomes", TENANT_ONLY
    ),
    PermissionDef(
        "bgv:manage",
        "Compliance",
        "Run BGV cases: start, assign, record checks, raise and resolve discrepancies",
        TENANT_ONLY,
    ),
    PermissionDef(
        "bgv:configure",
        "Compliance",
        "Configure BGV packages, providers and reference questionnaires",
        TENANT_ONLY,
    ),
    PermissionDef(
        "reference_check:request", "Compliance", "Request and review reference checks", TENANT_ONLY
    ),
]
# Epic 25 (timesheets, FR-128..FR-133). Default-role grants: ``_EPIC25_ROLE_GRANTS`` below (Phase 2 roles only;
# the frozen P1 roles receive them at the phase-close resync, AD-16). Approver steps still need route membership.
_PERMISSIONS += [
    PermissionDef("timesheet:read", "Timesheets", "View timesheets, entries and approval history"),
    PermissionDef(
        "timesheet:configure",
        "Timesheets",
        "Configure timesheet settings, Client defaults and placement configs",
        TENANT_ONLY,
    ),
    PermissionDef(
        "timesheet:admin",
        "Timesheets",
        "Proxy entry, import, reopen, adjustments and offline Client approval",
        TENANT_ONLY,
    ),
    PermissionDef("timesheet:approve", "Timesheets", "Decide internal timesheet approval steps"),
    PermissionDef("timesheet:monitor", "Timesheets", "Monitor missing / late timesheets and send reminders"),
]
# Epic 27 (expenses, FR-137..FR-139). Default-role grants: ``_EPIC27_ROLE_GRANTS`` below (Phase 2 roles only;
# the frozen P1 roles receive them at the phase-close resync, AD-16). Approver steps still need route membership.
_PERMISSIONS += [
    PermissionDef("expense:read", "Expenses", "View expense claims, lines, receipts and approval history"),
    PermissionDef(
        "expense:policy",
        "Expenses",
        "Manage expense policies, Client overrides and expense settings",
        TENANT_ONLY,
    ),
    PermissionDef(
        "expense:admin",
        "Expenses",
        "Proxy expense claims, proxy submit and reopen approved claims",
        TENANT_ONLY,
    ),
    PermissionDef("expense:approve", "Expenses", "Decide internal expense claim approval steps"),
]
# Epic 26 (leave & holidays, FR-134..FR-136). Default-role grants: ``_EPIC26_ROLE_GRANTS`` below (Phase 2 roles
# only; the frozen P1 Account Manager receives ``leave:read|approve|read_team`` and ``holiday:read`` at the
# phase-close resync, AD-16). Approver steps still need route membership; balance adjustments are audited.
_PERMISSIONS += [
    PermissionDef("holiday:read", "Leave", "View holiday calendars and placement holidays"),
    PermissionDef("holiday:manage", "Leave", "Create, edit, copy and archive holiday calendars", TENANT_ONLY),
    PermissionDef("leave:read", "Leave", "View leave requests, balances and ledger history"),
    PermissionDef(
        "leave:manage_types", "Leave", "Manage leave types, Client overrides and leave settings", TENANT_ONLY
    ),
    PermissionDef(
        "leave:adjust_balance", "Leave", "Adjust leave balances (audited) and run accruals", TENANT_ONLY
    ),
    PermissionDef("leave:approve", "Leave", "Decide internal leave approval steps and raise proxy requests"),
    PermissionDef("leave:read_team", "Leave", "View the team leave calendar"),
]
# Epic 29 (billing & GST invoicing, FR-143..FR-152). ``billing:read|write`` are Epic 16's and ``invoice:issue`` is
# Epic 14's (referenced only). Default-role grants: ``_EPIC29_ROLE_GRANTS`` below; never granted to recruiters.
_PERMISSIONS += [
    PermissionDef(
        "billing:configure", "Finance", "Configure SAC codes, GST rates and billing settings", TENANT_ONLY
    ),
    PermissionDef("credit_note:write", "Finance", "Raise credit and debit notes", TENANT_ONLY),
    PermissionDef("einvoice:write", "Finance", "Record, import and cancel e-invoice IRNs", TENANT_ONLY),
]
# Epic 34 (dashboards & reporting, A1): exporting finance lists (invoices, payments, expense claims) with
# amounts needs this in addition to the kind's read permission. Finance Admin default (AD-16, no resync).
_PERMISSIONS += [
    PermissionDef("report:finance_export", "Reports", "Export finance lists with amounts", TENANT_ONLY)
]
# Epic 28 (payroll export, FR-140..FR-142). ``payroll:read|generate|finalize`` are Epic 17's (referenced only).
# ``period:reopen_pay`` is sensitive (Finance Admin only). Default-role grants: ``_EPIC28_ROLE_GRANTS`` below.
_PERMISSIONS += [
    PermissionDef(
        "payroll:configure",
        "Finance",
        "Configure pay calendars, payroll file mappings and the payroll connector",
        TENANT_ONLY,
    ),
    PermissionDef(
        "period:reopen_pay", "Finance", "Reopen a locked pay period (sensitive)", TENANT_ONLY, sensitive=True
    ),
]
SENSITIVE_PERMISSIONS = SENSITIVE_PERMISSIONS | {"period:reopen_pay"}
# Epic 31 (extension, conversion & offboarding, FR-162..FR-166). Catalogue only, like Epics 19 / 24: the Tenant
# Admin holds them dynamically; ★ ``extension:confirm`` and ``offboarding:close`` stay human-only (status machines).
_PERMISSIONS += [
    PermissionDef(
        "extension:write",
        "Workforce",
        "Propose, withdraw and record client responses to placement extensions",
    ),
    PermissionDef("extension:confirm", "Workforce", "Confirm placement extensions", TENANT_ONLY),
    PermissionDef("conversion:write", "Workforce", "Record contract-to-hire conversions", TENANT_ONLY),
    PermissionDef(
        "offboarding:write", "Workforce", "Start offboarding and early terminations, complete checklist items"
    ),
    PermissionDef(
        "offboarding:close", "Workforce", "Close offboarding cases and waive mandatory items", TENANT_ONLY
    ),
    PermissionDef(
        "offboarding:configure", "Workforce", "Configure offboarding templates and exit settings", TENANT_ONLY
    ),
]
# Epic 30 (receivables, collections & period close, FR-153..FR-161). ``payment:record`` / ``invoice:write_off`` are
# Epic 14's and ``period:close`` the foundation's (referenced only). Grants: ``_EPIC30_ROLE_GRANTS`` below.
_PERMISSIONS += [
    PermissionDef(
        "receivables:read", "Finance", "View payments, aging, collections, statements and TDS", TENANT_ONLY
    ),
    PermissionDef(
        "payment:reverse",
        "Finance",
        "Reverse payments and allocations (sensitive)",
        TENANT_ONLY,
        sensitive=True,
    ),
    PermissionDef(
        "collections:write", "Finance", "Log collection activities and send payment reminders", TENANT_ONLY
    ),
    PermissionDef("dispute:write", "Finance", "Open and resolve invoice disputes", TENANT_ONLY),
    PermissionDef("tds:write", "Finance", "Record TDS certificates and TDS sections", TENANT_ONLY),
    PermissionDef(
        "period:reopen",
        "Finance",
        "Request the reopening of a closed accounting period (sensitive)",
        TENANT_ONLY,
        sensitive=True,
    ),
    PermissionDef("accounting:export", "Finance", "Export the accounting ledger", TENANT_ONLY),
    PermissionDef(
        "reconciliation:read", "Finance", "View receivables reconciliation runs and findings", TENANT_ONLY
    ),
]
SENSITIVE_PERMISSIONS = SENSITIVE_PERMISSIONS | {"payment:reverse", "period:reopen"}
# Epic 35 (privacy platform, A1): go-live data import (Tenant Admin holds every catalog permission; no other
# default role). ``retention:manage`` and ``audit:read`` are P1 and only gain endpoints.
_PERMISSIONS += [
    PermissionDef(
        "golive:import",
        "Administration",
        "Import go-live data (workers, placements, opening balances)",
        TENANT_ONLY,
    )
]
# Epic 50 (Client Portal SSO, FR-250..FR-252, AD-48): per-Client identity providers, enforcement, break-glass.
# Default-role definitions: Account Manager (``_EPIC50_ROLE_GRANTS`` below) and the Tenant Admin (dynamic). No resync.
_PERMISSIONS += [
    PermissionDef(
        "client_sso:manage",
        "Clients",
        "Configure a client's single sign-on, enforcement and break-glass users",
        TENANT_ONLY,
    )
]

# Epic 37 (skills graph & proficiency, FR-187..FR-191, AD-41). ``skill:verify`` and ``skills_insight:read`` are
# data-scoped like ``candidate:update``. Default-role definitions: ``_EPIC37_ROLE_GRANTS`` below. No resync.
_PERMISSIONS += [
    PermissionDef(
        "skill_graph:manage",
        "Candidates",
        "Manage tenant skill relationships and skills graph settings",
        TENANT_ONLY,
    ),
    PermissionDef("skill:verify", "Candidates", "Verify candidate skill proficiency (recruiter evidence)"),
    PermissionDef("skills_insight:read", "Reports", "View the skills supply and demand insight"),
]
# Epic 51 (finance carry-overs, FR-253..FR-255). Reading recon results uses ``receivables:read``; FX settings read
# uses ``legal_entity:read``. Default-role grants: ``_EPIC51_ROLE_GRANTS`` below (definitions only, no resync).
_PERMISSIONS += [
    PermissionDef(
        "dsc:configure",
        "Finance",
        "Configure DSC signing policies and handle pending signatures",
        TENANT_ONLY,
    ),
    PermissionDef(
        "tds_recon:write", "Finance", "Run 26AS / AIS TDS reconciliation and decide matches", TENANT_ONLY
    ),
    PermissionDef("fx_feed:configure", "Finance", "Configure the live FX rate feed", TENANT_ONLY),
]

# Epic 46 (immigration, FR-232..FR-236, AD-52). ``immigration:read`` / ``manage`` are data-scoped on the Case
# owner / BU; ``configure`` (visa types, counsel) is tenant-wide. Unmasking numbers also needs ``identity:view``.
# Default-role definitions: the new Immigration Coordinator role and Account Manager (``_EPIC46_*`` below). No resync.
_PERMISSIONS += [
    PermissionDef(
        "immigration:read", "Immigration", "View immigration cases, permits and the immigration dashboard"
    ),
    PermissionDef(
        "immigration:manage", "Immigration", "Open and run immigration cases, checklists and outcomes"
    ),
    PermissionDef(
        "immigration:configure", "Immigration", "Configure visa types and immigration counsel", TENANT_ONLY
    ),
]
# Epic 45 (SOW & project staffing, FR-227..FR-231). ``project:read`` / ``project:write`` are data-scoped on the
# project's owner / business unit; ``deliverable:record`` records a Client's offline acceptance (with evidence).
# Default-role definitions: ``_EPIC45_ROLE_GRANTS`` below. No resync.
_PERMISSIONS += [
    PermissionDef(
        "project:read", "Projects", "View projects, roles, assignments, deliverables, budgets and burn"
    ),
    PermissionDef(
        "project:write", "Projects", "Create and edit projects, roles, assignments, deliverables and budgets"
    ),
    PermissionDef(
        "deliverable:record",
        "Projects",
        "Record a Client's offline deliverable acceptance or rejection",
        TENANT_ONLY,
    ),
]
# Epic 47 (custom report builder & scheduled reports, FR-237..FR-240). Viewing a shared report uses
# ``report:read``; exporting uses ``report:export`` (+ ``report:finance_export`` for finance sources).
# Default-role definitions: ``_EPIC47_ROLE_GRANTS`` below (Tenant Admin holds all three dynamically). No resync.
_PERMISSIONS += [
    PermissionDef(
        "report:build",
        "Reports",
        "Build and edit your own custom reports (team sharing included)",
        TENANT_ONLY,
    ),
    PermissionDef(
        "report:share_tenant", "Reports", "Share custom reports tenant-wide or with a role", TENANT_ONLY
    ),
    PermissionDef(
        "report:schedule", "Reports", "Schedule custom reports you own for internal users", TENANT_ONLY
    ),
]
# Epic 38 (talent pools & rediscovery, FR-192..FR-195). Pool visibility (private / team / tenant / shared) is the
# Pool's own rule, so the keys are tenant-wide; members are always re-filtered by the viewer's ``candidate:read``
# scope. Default-role definitions: ``_EPIC38_ROLE_GRANTS`` below (Tenant Admin holds all four dynamically). No resync.
_PERMISSIONS += [
    PermissionDef("pool:read", "Candidates", "View talent pools and segments visible to you", TENANT_ONLY),
    PermissionDef(
        "pool:write",
        "Candidates",
        "Create and edit your own talent pools, members and demand pins",
        TENANT_ONLY,
    ),
    PermissionDef(
        "pool:manage",
        "Candidates",
        "Manage tenant-wide talent pools, pool templates and any pool's refresh",
        TENANT_ONLY,
    ),
    PermissionDef("segment:write", "Candidates", "Create and edit candidate / worker segments", TENANT_ONLY),
]
# Epic 42 (employee referrals, FR-213..FR-216). ``referral:read`` / ``referral:manage`` are data-scoped through the
# referral's Demand (owner / BU); general referrals need tenant scope. Referring and viewing your own referrals needs
# no key (``/me/referrals``, ``/portal/work/referrals``). ``referral_reward:approve`` is sensitive (finance).
# Default-role definitions: ``_EPIC42_ROLE_GRANTS`` below. No resync.
_PERMISSIONS += [
    PermissionDef("referral:read", "Referrals", "View referrals and their rewards"),
    PermissionDef(
        "referral:manage", "Referrals", "Link referrals to openings, void rewards and mark payouts paid"
    ),
    PermissionDef(
        "referral:configure", "Referrals", "Configure referral programs and referral settings", TENANT_ONLY
    ),
    PermissionDef(
        "referral_reward:approve",
        "Referrals",
        "Approve referral rewards (sensitive)",
        TENANT_ONLY,
        sensitive=True,
    ),
]
SENSITIVE_PERMISSIONS = SENSITIVE_PERMISSIONS | {"referral_reward:approve"}
# Epic 40 (assessments, FR-202..FR-208). Reads / invites / reviews are tenant-wide within the permission (the
# Candidate 360 results also apply the candidate read scope); ``configure`` is tenant-wide. Pass / Fail of a gating
# invitation is human only (FR-257). Default-role definitions: ``_EPIC40_ROLE_GRANTS`` below. No resync.
_PERMISSIONS += [
    PermissionDef(
        "assessment:read", "Assessments", "View assessment results, invitations and proctoring signals"
    ),
    PermissionDef(
        "assessment:author",
        "Assessments",
        "Author assessment items, import items, assemble and publish assessments",
    ),
    PermissionDef(
        "assessment:invite", "Assessments", "Invite candidates to assessments, extend and cancel invitations"
    ),
    PermissionDef(
        "assessment:review", "Assessments", "Review answers, record scores and Pass / Fail, annotate signals"
    ),
    PermissionDef(
        "assessment:configure",
        "Assessments",
        "Configure assessment settings and provider connectors",
        TENANT_ONLY,
    ),
]
# Epic 48 (predictive analytics, FR-241..FR-245). ``prediction:read`` shows time-to-fill and offer-drop signals,
# ``prediction:read_attrition`` (sensitive, FR-243) the attrition risk of Active Placements; both also need read access
# to the subject. ``model:manage`` (model cards, enable / disable, thresholds) is tenant-wide. Default-role definitions:
# ``_EPIC48_ROLE_GRANTS`` below. No resync.
_PERMISSIONS += [
    PermissionDef(
        "prediction:read", "Analytics", "View time-to-fill and offer-drop predictions (supporting signals)"
    ),
    PermissionDef(
        "prediction:read_attrition",
        "Analytics",
        "View attrition risk predictions on active placements",
        sensitive=True,
    ),
    PermissionDef(
        "model:manage",
        "Analytics",
        "Manage predictive models: model cards, enable or disable, data thresholds",
        TENANT_ONLY,
    ),
]
SENSITIVE_PERMISSIONS = SENSITIVE_PERMISSIONS | {"prediction:read_attrition"}
# Epic 49 (bias audit, FR-246..FR-249). Aggregate-only reports and packs (``fairness:read``), settings / LL144 /
# publications (``fairness:configure``) and the AEDT inventory (``aedt:manage``); all tenant-wide. No permission ever
# reads an individual's self-ID. Default-role definitions: ``_EPIC49_ROLE_GRANTS`` below. No resync.
_PERMISSIONS += [
    PermissionDef(
        "fairness:read",
        "Compliance",
        "Run and view aggregate adverse-impact reports and auditor packs",
        TENANT_ONLY,
    ),
    PermissionDef(
        "fairness:configure",
        "Compliance",
        "Configure self-ID, category labels, LL144 notices and publish summaries",
        TENANT_ONLY,
    ),
    PermissionDef(
        "aedt:manage",
        "Compliance",
        "Manage the automated employment decision tool (AEDT) inventory",
        TENANT_ONLY,
    ),
]
# Epic 43 (campus hiring & recruitment events, FR-217..FR-221). ``campus:read`` / ``campus:manage`` are data-scoped on
# the drive owner; ``event:manage`` (events, stations, kiosk, queue) on the event owner. The eligibility override is
# human only (FR-257, star). Default-role definitions: the new Campus Recruiter role and Recruitment Manager
# (``_EPIC43_*`` below). No resync.
_PERMISSIONS += [
    PermissionDef(
        "campus:read", "Campus", "View institutions, campus drives, registrations and the campus funnel"
    ),
    PermissionDef(
        "campus:manage",
        "Campus",
        "Manage institutions, campus drives, registration links, shortlists and interview batches",
    ),
    PermissionDef(
        "event:manage",
        "Campus",
        "Run recruitment events: registration, check-in, stations and the interview queue",
    ),
    PermissionDef(
        "campus_eligibility:override",
        "Campus",
        "Override a campus registration's eligibility (with a reason)",
    ),
]
# Epic 41 (internal mobility, FR-209..FR-212). Browsing the internal board and applying needs no key (any active
# internal User; Workers on ``/portal/work/opportunities``). ``mobility:manage`` (board settings, eligibility rules,
# employee grades) and ``mobility:transfer`` (confirm a transfer, human only, star) are data-scoped through the Demand.
# Default-role definitions: ``_EPIC41_ROLE_GRANTS`` below. No resync.
_PERMISSIONS += [
    PermissionDef(
        "mobility:manage",
        "Internal mobility",
        "Manage the internal job board, eligibility rules and employee grades",
    ),
    PermissionDef("mobility:transfer", "Internal mobility", "Confirm internal transfers of employees"),
]
# Epic 39 (recruitment marketing, FR-196..FR-201). Campaign / sequence keys are tenant-wide (the audience is always
# re-filtered by the viewer's ``candidate:read`` scope); ``campaign:send`` schedules / starts / resumes below the
# approval threshold; ``marketing:configure`` (frequency cap, send windows, throttle, tenant resume) is tenant-only.
# Default-role definitions: the new Recruitment Marketer role and ``_EPIC39_ROLE_GRANTS`` below. No resync.
_PERMISSIONS += [
    PermissionDef(
        "campaign:read",
        "Marketing",
        "View audiences, campaigns, sequences and marketing analytics",
        TENANT_ONLY,
    ),
    PermissionDef(
        "campaign:write",
        "Marketing",
        "Build audiences and campaign drafts, submit and cancel campaigns",
        TENANT_ONLY,
    ),
    PermissionDef(
        "campaign:approve", "Marketing", "Approve campaigns above the approval threshold", TENANT_ONLY
    ),
    PermissionDef("campaign:send", "Marketing", "Schedule, pause and resume campaigns", TENANT_ONLY),
    PermissionDef("sequence:write", "Marketing", "Enrol candidates in nurture sequences", TENANT_ONLY),
    PermissionDef(
        "marketing:configure",
        "Marketing",
        "Configure marketing frequency caps, send windows and throttles",
        TENANT_ONLY,
    ),
]
# Epic 44 (workforce planning, FR-222..FR-226). ``workforce_plan:read`` / ``write`` / ``approve`` are data-scoped on the
# plan's owner / business unit; approving is human only (FR-257). Default-role definitions: ``_EPIC44_ROLE_GRANTS``
# below. No resync.
_PERMISSIONS += [
    PermissionDef(
        "workforce_plan:read",
        "Workforce planning",
        "View workforce plans, headcount slots and plan vs actual",
    ),
    PermissionDef(
        "workforce_plan:write",
        "Workforce planning",
        "Create and edit workforce plans, import lines, budget, submit and convert slots",
    ),
    PermissionDef(
        "workforce_plan:approve", "Workforce planning", "Approve workforce plans and plan revisions"
    ),
]
# Epic 53 (Phase 4 foundation, FR-328): AI governance and agent tool-level keys. ``ai:govern`` / ``ai:agent_configure`` /
# ``ai:kill_switch`` are tenant-wide and sensitive; ``proposal:decide`` and the message keys are data-scoped;
# ``knowledge:read`` is declared now because the ``knowledge.search`` core tool needs it (Epic 62 owns the feature).
# Default-role definitions: ``_EPIC53_ROLE_GRANTS`` below. No resync (the Phase 4 close resyncs).
_PERMISSIONS += [
    PermissionDef(
        "ai:govern",
        "AI agents",
        "View the AI governance console, runs and payloads (with reason)",
        TENANT_ONLY,
        sensitive=True,
    ),
    PermissionDef(
        "ai:agent_configure",
        "AI agents",
        "Enable agents, set autonomy, routing, budgets, caps and triggers",
        TENANT_ONLY,
        sensitive=True,
    ),
    PermissionDef(
        "ai:kill_switch", "AI agents", "Turn the agent kill switch on or off", TENANT_ONLY, sensitive=True
    ),
    # C-14: reading stored prompt / output payloads needs this key + a reason (audited); Tenant Admin only by default.
    PermissionDef(
        "ai:payload_read",
        "AI agents",
        "Read stored AI agent prompts and outputs (with a reason, audited)",
        TENANT_ONLY,
        sensitive=True,
    ),
    PermissionDef("proposal:decide", "AI agents", "Approve, edit or reject AI agent Proposals routed to you"),
    PermissionDef("message:draft", "AI agents", "Let agents draft messages for you"),
    PermissionDef("message:send_templated", "AI agents", "Let agents send approved templates on your behalf"),
    PermissionDef("knowledge:read", "Knowledge", "Read Knowledge Sources (Onboarding Q&A)"),
]
# Epic 54 (Recruitment Copilot, FR-276): the side panel. Answers stay within the user's own Effective Rights.
_PERMISSIONS += [
    PermissionDef(
        "copilot:use",
        "AI agents",
        "Use the Copilot side panel (answers stay within your own access)",
        TENANT_ONLY,
    ),
]
# Epic 57 (Screening Agent, FR-289..FR-292): AI screening conversations, answer review, questionnaire versions.
_PERMISSIONS += [
    PermissionDef(
        "screening:conduct",
        "Screening",
        "Run AI screening conversations, review agent answers and manage screening questionnaires",
    ),
]
# Epic 58 (Voice Screening, FR-293..FR-296): request voice screens and upload call recordings / transcripts.
_PERMISSIONS += [
    PermissionDef(
        "voice:upload",
        "Screening",
        "Request voice screens and upload call recordings or transcripts",
        sensitive=True,
    ),
]
# Epic 59 (Scheduling Agent, FR-297): interviewers' free/busy only (never event content) for slot proposals.
_PERMISSIONS += [
    PermissionDef(
        "calendar:free_busy",
        "Interviews",
        "Read interviewers' free/busy times (never event content) to propose interview slots",
    ),
]
# Epic 60 (Interview Assistant, FR-301..FR-304): consented interview capture, uploads and the assistant.
_PERMISSIONS += [
    PermissionDef(
        "interview_capture:manage",
        "Interviews",
        "Request interview capture, upload interview recordings or transcripts and use the Interview Assistant",
        sensitive=True,
    ),
]
# Epic 61 (Submission Agent, FR-305..FR-307): agents prepare internal draft submission packs; a person sends.
_PERMISSIONS += [
    PermissionDef("submission:draft", "AI agents", "Let agents prepare draft submission packs for you"),
]
# Epic 62 (Compliance, BGV & Onboarding Agents, FR-308..FR-310): Knowledge Sources and BGV provider follow-ups.
_PERMISSIONS += [
    PermissionDef(
        "knowledge:manage", "Knowledge", "Create, edit, publish and unpublish Knowledge Sources", TENANT_ONLY
    ),
    PermissionDef("bgv:follow_up", "AI agents", "Let agents follow up BGV providers for you"),
]
# Epic 63 (Timesheet & Billing/Collections Agents, FR-311..FR-315): agents prepare invoice runs for a Finance User.
_PERMISSIONS += [
    PermissionDef("billing:prepare", "AI agents", "Let agents prepare invoice runs for you"),
]
# Epic 66 (Phase 5 foundation, FR-331..FR-341): Installations, the Ecosystem Kill Switch, private Apps and usage meters.
_PERMISSIONS += [
    PermissionDef("integration:view", "Integrations", "View the marketplace, Installations and their health", TENANT_ONLY),
    PermissionDef(
        "integration:manage",
        "Integrations",
        "Install, configure, grant and uninstall Apps and connectors; set the app policy",
        TENANT_ONLY,
        sensitive=True,
    ),
    PermissionDef(
        "ecosystem:kill_switch", "Integrations", "Suspend or restore all external access", TENANT_ONLY, sensitive=True
    ),
    PermissionDef("app:develop", "Integrations", "Register and manage the Tenant's private Apps", TENANT_ONLY, sensitive=True),
    PermissionDef("usage:read", "Integrations", "View API, webhook and connector usage meters", TENANT_ONLY),
]
SENSITIVE_PERMISSIONS = SENSITIVE_PERMISSIONS | {"integration:manage", "ecosystem:kill_switch", "app:develop"}
# Epic 68 (Webhooks, FR-349..FR-353): tenant-owned webhook subscriptions (sensitive: tenant-wide event metadata leaves
# the platform). Not NON_DELEGABLE: no scope maps it; Apps use the scope ``webhooks:manage`` (own Subscriptions only).
_PERMISSIONS += [
    PermissionDef(
        "webhook:manage", "Integrations", "Create, verify, rotate, replay and delete webhook subscriptions", TENANT_ONLY,
        sensitive=True,
    ),
]
SENSITIVE_PERMISSIONS = SENSITIVE_PERMISSIONS | {"webhook:manage"}
# Epic 76 (Real Telephony & STT, FR-391..FR-395): recruiter-initiated bridged calls (★ human only, never delegable).
_PERMISSIONS += [
    PermissionDef("voice:call", "Screening", "Place screening calls through the calling provider", sensitive=True),
]
SENSITIVE_PERMISSIONS = SENSITIVE_PERMISSIONS | {"voice:call"}
# Epic 73 (Governed MCP Server, FR-375..FR-380): a user's own gate for connecting an external AI assistant.
_PERMISSIONS += [
    PermissionDef(
        "mcp:use", "AI agents", "Connect an external AI assistant to Talent OS (it acts only within your own access)",
        TENANT_ONLY,
    ),
]
# Epic 70 (Job Board Connectors, FR-360..FR-364): publishing to external job boards (user actions only).
_PERMISSIONS += [
    PermissionDef(
        "posting:publish_external", "Demands", "Publish job postings to external job boards and manage board postings",
        ALL_SCOPES,
    ),
]
# Epic 75 (Cross-tenant Job Network, FR-386..FR-390): join the public Talent OS Jobs network and set listing defaults.
# Sensitive: it publishes careers-public jobs on the Tenant's behalf to a platform surface. ``_EPIC75_ROLE_GRANTS`` is
# empty on purpose -- joining is a Tenant Admin decision (the system Tenant Admin holds every key implicitly); analytics
# are also readable with ``report:read``; ``posting:publish_external`` (Epic 70) governs per-posting opt-in.
_PERMISSIONS += [
    PermissionDef(
        "network:manage", "Demands", "Join the Talent OS Jobs network and set listing defaults", TENANT_ONLY,
        sensitive=True,
    ),
]
SENSITIVE_PERMISSIONS = SENSITIVE_PERMISSIONS | {"network:manage"}
# Epic 71 (HRMS & Accounting Connectors, FR-365..FR-370): HRMS field mapping / pushes and ledger sync (sensitive:
# finance documents leave the platform; only Finance turns on PAN / bank / Aadhaar-reference HRMS mappings).
_PERMISSIONS += [
    PermissionDef("hrms:sync", "Placements", "Map HRMS fields and push Workers to the HR system", ALL_SCOPES),
    PermissionDef(
        "ledger:sync", "Finance",
        "Map ledgers, push finance documents to the accounting system and review payment suggestions", ALL_SCOPES,
        sensitive=True,
    ),
]  # fmt: skip
SENSITIVE_PERMISSIONS = SENSITIVE_PERMISSIONS | {"ledger:sync"}
# Epic 78 (AI campaign copy, FR-401..FR-403): approving AI-drafted copy is a human-only decision (★, never delegable).
_PERMISSIONS += [
    PermissionDef("campaign:copy_approve", "Marketing", "Approve AI-drafted campaign copy"),
]
SENSITIVE_PERMISSIONS = SENSITIVE_PERMISSIONS | {
    "ai:govern",
    "ai:agent_configure",
    "ai:kill_switch",
    "ai:payload_read",
    "interview_capture:manage",  # Epic 60
    "voice:upload",  # Epic 58
}
CATALOG: dict[str, PermissionDef] = {p.key: p for p in _PERMISSIONS}
if len(CATALOG) != len(_PERMISSIONS):  # pragma: no cover - guards edits to the list above
    msg = "duplicate permission key in the catalog"
    raise RuntimeError(msg)

ALL_PERMISSIONS: frozenset[str] = frozenset(CATALOG)
COMPENSATION_VIEW = "compensation:view"

# Epic 66 (AD-75, FR-332/FR-348): permissions never delegated to an API key or an App scope (moved here from
# ``api_keys_service``, which re-exports it).
NON_DELEGABLE: frozenset[str] = frozenset(
    {
        "api_key:manage",
        "support_access:approve",
        "dsr:process",
        "role:manage",
        "user:manage",
        "settings:manage",
        "privacy:manage",
        "retention:manage",
        # Epic 66
        "integration:manage",
        "ecosystem:kill_switch",
        "ai:kill_switch",
        "ai:agent_configure",
        "proposal:decide",
        "ai:payload_read",
        "app:develop",
        "voice:call",  # Epic 76 (★ placing a telephony call)
        "campaign:copy_approve",  # Epic 78 (★ approving AI-drafted campaign copy)
        "network:manage",  # Epic 75 (join the Talent OS Jobs network; never delegated to an App / API key)
    }
)
# Epic 66 (AD-75, FR-341): permissions whose only use is a human-only (★) decision. No App scope may map one
# (``core.oauth_scopes.validate_catalogue``); CI cross-checks the list against the P1-P4 ★ guards.
_STAR_CANDIDATES = (
    "application:reject", "offer:release", "offer:approve", "offer:withdraw", "interview:outcome", "invoice:issue",
    "invoice:write_off", "payment:record", "payment:reverse", "placement:join", "placement:drop", "demand:approve",
    "campaign:approve", "compliance:waive", "proposal:decide", "timesheet:approve", "period:reopen", "period:reopen_pay",
    "referral_reward:approve",
    "campaign:copy_approve",  # Epic 78
)  # fmt: skip
STAR_PERMISSIONS: frozenset[str] = frozenset(p for p in _STAR_CANDIDATES if p in CATALOG)


class InvalidGrant(ValueError):  # noqa: N818 - domain name
    def __init__(self, permission: str, reason: str) -> None:
        self.permission = permission
        self.reason = reason
        super().__init__(f"{permission}: {reason}")


def validate_grants(grants: Mapping[str, str]) -> dict[str, Scope]:
    """Normalize a role's ``{permission: scope}`` map; raises ``InvalidGrant`` on unknown/illegal entries."""
    out: dict[str, Scope] = {}
    for perm, raw_scope in grants.items():
        definition = CATALOG.get(perm)
        if definition is None:
            raise InvalidGrant(perm, "unknown permission")
        try:
            scope = Scope(raw_scope)
        except ValueError as exc:
            raise InvalidGrant(perm, f"unknown scope '{raw_scope}'") from exc
        if scope not in definition.scopes:
            allowed = ", ".join(s.value for s in definition.scopes)
            raise InvalidGrant(perm, f"scope '{scope.value}' not allowed (allowed: {allowed})")
        out[perm] = scope
    return out


def merge_grants(grant_maps: Iterable[Mapping[str, str]]) -> dict[str, Scope]:
    """Union of several roles' grants, keeping the widest scope per permission (unknown keys ignored)."""
    merged: dict[str, Scope] = {}
    for grants in grant_maps:
        for perm, raw in grants.items():
            if perm not in CATALOG:
                continue
            try:
                scope = Scope(raw)
            except ValueError:
                continue
            merged[perm] = widest(merged[perm], scope) if perm in merged else scope
    return merged


# ================================================================ default roles
@dataclasses.dataclass(frozen=True, slots=True)
class RoleTemplate:
    key: str
    name: str
    description: str
    permissions: Mapping[str, Scope]


TENANT_ADMIN = "tenant_admin"

_T, _B, _M, _O = Scope.TENANT, Scope.BU, Scope.TEAM, Scope.OWN


def _grant(scope: Scope, *perms: str) -> dict[str, Scope]:
    for p in perms:
        if p not in CATALOG:  # pragma: no cover - guards edits below
            msg = f"default role references unknown permission {p}"
            raise RuntimeError(msg)
    return dict.fromkeys(perms, scope)


# Organization directory (teams, business units). Managers additionally read users (``user:read`` opens the
# user administration list, so recruiters and viewers do not get it).
_DIRECTORY_READ = ("team:read", "business_unit:read")

DEFAULT_ROLES: tuple[RoleTemplate, ...] = (
    # Everything, tenant-wide. Its permissions are immutable (see identity.rbac).
    RoleTemplate(
        TENANT_ADMIN,
        "Tenant Admin",
        "Full access to every feature and all administration.",
        dict.fromkeys(CATALOG, Scope.TENANT),
    ),
    # Runs recruitment for a business unit: full pipeline control in the BU, talent pool tenant-wide,
    # approves demands and offers, sees compensation.
    RoleTemplate(
        "recruitment_manager",
        "Recruitment Manager",
        "Leads recruiting in a business unit: pipeline, approvals, reports and compensation.",
        {
            **_grant(_T, "client:read", "contact:read", "candidate:read", "candidate:create"),
            **_grant(_T, "compensation:view", "role:read", "user:read", "note:write", *_DIRECTORY_READ),
            **_grant(_B, "demand:read", "demand:update", "demand:assign", "demand:approve"),
            **_grant(_B, "candidate:update", "candidate:merge", "candidate:delete"),
            **_grant(_B, "application:read", "application:create", "application:update"),
            **_grant(_B, "application:move", "application:delete", "application:reject", "screening:write"),
            **_grant(_B, "submission:read", "submission:create", "submission:update"),
            **_grant(_B, "interview:read", "interview:create", "interview:update", "interview:delete"),
            **_grant(_B, "scorecard:read", "offer:read", "offer:create", "offer:update", "offer:approve"),
            **_grant(_B, "offer:write", "offer:release", "offer:withdraw"),
            **_grant(_B, "interview:schedule", "interview:outcome", "scorecard:submit"),
            **_grant(_T, "interview:configure"),
            **_grant(_O, "connection:manage_own"),
            **_grant(_B, "task:read", "task:create", "task:update", "task:delete"),
            **_grant(_O, "email:send"),
            **_grant(_T, "email_template:read"),
            **_grant(_B, "report:read", "report:export"),
            **_grant(_T, "match:read", "match:write"),
        },
    ),
    # Owns delivery of client demands in a business unit: creates/approves demands, manages offers.
    RoleTemplate(
        "delivery_manager",
        "Delivery Manager",
        "Delivers client demands in a business unit: demands, submissions, interviews and offers.",
        {
            **_grant(_T, "candidate:read", "compensation:view", "user:read", "note:write", *_DIRECTORY_READ),
            **_grant(_B, "client:read", "contact:read", "contact:create", "contact:update"),
            **_grant(_B, "demand:read", "demand:create", "demand:update", "demand:assign", "demand:approve"),
            **_grant(_B, "application:read", "application:update", "application:move", "application:reject"),
            **_grant(_B, "screening:write"),
            **_grant(_B, "submission:read", "submission:create", "submission:update"),
            **_grant(_B, "interview:read", "interview:create", "interview:update", "scorecard:read"),
            **_grant(_B, "interview:schedule", "interview:outcome", "scorecard:submit"),
            **_grant(_O, "connection:manage_own"),
            **_grant(_B, "offer:read", "offer:create", "offer:update"),
            **_grant(_B, "offer:write", "offer:release", "offer:withdraw"),
            **_grant(_B, "task:read", "task:create", "task:update"),
            **_grant(_O, "email:send"),
            **_grant(_T, "email_template:read"),
            **_grant(_B, "report:read", "report:export"),
            **_grant(_T, "match:read", "match:write"),
        },
    ),
    # Owns client relationships: manages their own clients/contacts and raises demands for them.
    RoleTemplate(
        "account_manager",
        "Account Manager",
        "Manages own client accounts and contacts and raises demands for them.",
        {
            **_grant(_T, "client:read", "contact:read", "client:create", "contact:create"),
            **_grant(_T, "candidate:read", "compensation:view", "note:write", *_DIRECTORY_READ),
            **_grant(_O, "client:update", "client:delete", "contact:update", "contact:delete"),
            **_grant(_O, "demand:read", "demand:create", "demand:update"),
            **_grant(_M, "application:read", "submission:read", "submission:update", "interview:read"),
            **_grant(_M, "offer:read"),
            **_grant(_O, "scorecard:submit", "connection:manage_own"),
            **_grant(_O, "task:read", "task:create", "task:update", "report:read"),
            **_grant(_O, "email:send"),
            **_grant(_T, "email_template:read"),
            **_grant(_T, "match:read"),
        },
    ),
    # Sources and moves candidates on the team's demands. No compensation visibility by default.
    RoleTemplate(
        "recruiter",
        "Recruiter",
        "Sources candidates and runs the pipeline for the team's demands.",
        {
            **_grant(_T, "client:read", "contact:read", "candidate:read", "candidate:create"),
            **_grant(_T, "note:write", *_DIRECTORY_READ),
            **_grant(_M, "demand:read", "candidate:update"),
            **_grant(_M, "application:read", "application:create", "application:update", "application:move"),
            **_grant(_M, "application:reject", "screening:write"),
            **_grant(_M, "submission:read", "submission:create"),
            **_grant(_M, "interview:read", "interview:create", "interview:update", "scorecard:read"),
            **_grant(_M, "interview:schedule", "interview:outcome", "scorecard:submit"),
            **_grant(_O, "connection:manage_own"),
            **_grant(_M, "offer:read"),
            **_grant(_O, "task:read", "task:create", "task:update", "task:delete", "report:read"),
            **_grant(_O, "email:send"),
            **_grant(_T, "email_template:read"),
            **_grant(_T, "match:read", "match:write"),
        },
    ),
    # Sees only what they are asked to interview and submits scorecards.
    RoleTemplate(
        "interviewer",
        "Interviewer",
        "Sees assigned interviews and submits scorecards.",
        {
            **_grant(_O, "interview:read", "scorecard:read", "scorecard:submit", "candidate:read"),
            **_grant(_O, "connection:manage_own"),
            **_grant(_O, "task:read", "task:update"),
        },
    ),
    # Read-only across the tenant (no compensation, no administration).
    RoleTemplate(
        "viewer",
        "Viewer",
        "Read-only access to recruiting data across the tenant.",
        _grant(
            _T,
            "client:read",
            "contact:read",
            "demand:read",
            "candidate:read",
            "application:read",
            "submission:read",
            "interview:read",
            "scorecard:read",
            "offer:read",
            "task:read",
            "report:read",
            "match:read",
            *_DIRECTORY_READ,
        ),
    ),
    # DPDP Grievance Officer (Epic 12): processes data subject requests; reads candidates and consents.
    RoleTemplate(
        GRIEVANCE_OFFICER := "grievance_officer",
        "Grievance Officer",
        "Handles data subject requests (access, correction, erasure, consent withdrawal).",
        _grant(_T, "dsr:read", "dsr:process", "consent:read", "candidate:read"),
    ),
)

# Epic 14 (FR-81): the six Phase 2 default Roles. Domain epics append their own keys to these definitions;
# existing tenants receive them at the phase-close resync (AD-16).
_CORE_READ = ("client:read", "contact:read", "candidate:read", "note:write", *_DIRECTORY_READ)
_OWN_TASKS = ("task:read", "task:create", "task:update")
DEFAULT_ROLES += (
    RoleTemplate(
        "sales",
        "Sales",
        "Develops client accounts: leads, opportunities and client contacts.",
        {
            **_grant(_T, *_CORE_READ, "client:create", "contact:create", "email_template:read"),
            **_grant(_O, "client:update", "contact:update", *_OWN_TASKS, "email:send", "report:read"),
            # Epic 15: own leads / opportunities (managers widen the scope on a custom role).
            **_grant(_O, "lead:read", "lead:write", "opportunity:read", "opportunity:write"),
        },
    ),
    RoleTemplate(
        "hr_onboarding",
        "HR / Onboarding",
        "Prepares joiners: pre-joining, documents, onboarding and identity data.",
        {
            **_grant(_T, *_CORE_READ, "offer:read", "identity:view", "email_template:read"),
            **_grant(_O, *_OWN_TASKS, "email:send", "report:read"),
        },
    ),
    RoleTemplate(
        "compliance",
        "Compliance",
        "Runs background verification, credentials and compliance checks.",
        {
            **_grant(_T, *_CORE_READ, "bgv:adjudicate", "identity:view"),
            **_grant(_O, *_OWN_TASKS, "report:read"),
        },
    ),
    RoleTemplate(
        "finance",
        "Finance",
        "Prepares billing and receivables work for review.",
        {
            **_grant(_T, "client:read", "contact:read", "legal_entity:read", "note:write", *_DIRECTORY_READ),
            **_grant(_T, "report:read", "report:export"),
            **_grant(_O, *_OWN_TASKS),
        },
    ),
    RoleTemplate(
        "finance_admin",
        "Finance Admin",
        "Owns finance: legal entities, numbering, invoices, payments, write-offs and period close.",
        {
            **_grant(_T, "client:read", "contact:read", "note:write", *_DIRECTORY_READ),
            **_grant(_T, "legal_entity:read", "legal_entity:write", "document_series:write", "fx_rate:write"),
            **_grant(
                _T, "identity:view", "invoice:issue", "payment:record", "invoice:write_off", "period:close"
            ),
            **_grant(_T, "executive_dashboard:read", "report:read", "report:export"),
            **_grant(_O, *_OWN_TASKS),
        },
    ),
    RoleTemplate(
        "vendor_manager",
        "Vendor Manager",
        "Manages staffing vendors, their users and their submissions.",
        {
            **_grant(_T, *_CORE_READ, "vendor_portal:manage", "email_template:read"),
            **_grant(_O, *_OWN_TASKS, "email:send", "report:read"),
        },
    ),
)

# Epic 17 (definitions only, AD-16: existing tenants receive them at the phase-close resync). Only the Phase 2
# roles are extended here; the frozen Phase 1 roles (Recruiter: ``vendor_submission:review``; Recruitment
# Manager: ``vendor:release`` + review; Delivery Manager: ``vendor:performance``) get them at the phase-close
# resync, like the other Phase 2 domain keys.
_EPIC17_ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "vendor_manager": ("vendor:read", "vendor:write", "vendor:release", "vendor:performance"),
    "finance": ("vendor:read", "payroll:read", "payroll:generate"),
    "finance_admin": ("vendor:read", "payroll:read", "payroll:generate", "payroll:finalize"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC17_ROLE_GRANTS[r.key])})
    if r.key in _EPIC17_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 21: Compliance gets every compliance key; HR / Onboarding reads, records and verifies.
_EPIC21_ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "compliance": (
        "compliance:read",
        "compliance:configure",
        "compliance:waive",
        "credential:write",
        "credential:verify",
    ),
    "hr_onboarding": ("compliance:read", "credential:write", "credential:verify"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC21_ROLE_GRANTS[r.key])})
    if r.key in _EPIC21_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 23 (definitions only, AD-16): HR / Onboarding reads, manages and waives onboarding.
_EPIC23_ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "hr_onboarding": ("onboarding:read", "onboarding:manage", "onboarding:waive")
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC23_ROLE_GRANTS[r.key])})
    if r.key in _EPIC23_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 20 (definitions only, AD-16): Compliance runs BGV (everything except configure; it already holds
# ``bgv:adjudicate``) and reads reference answers.
_EPIC20_ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "compliance": ("bgv:read", "bgv:manage", "reference_check:request", "scorecard:read")
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC20_ROLE_GRANTS[r.key])})
    if r.key in _EPIC20_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 25 (definitions only, AD-16): Finance reads, monitors and approves timesheet steps; Finance Admin also
# administers (reopen, adjustments, import, offline Client approval) and configures.
_EPIC25_ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "finance": ("timesheet:read", "timesheet:monitor", "timesheet:approve"),
    "finance_admin": (
        "timesheet:read",
        "timesheet:monitor",
        "timesheet:approve",
        "timesheet:admin",
        "timesheet:configure",
    ),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC25_ROLE_GRANTS[r.key])})
    if r.key in _EPIC25_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 27 (definitions only, AD-16): Finance reads and approves expense steps; Finance Admin also manages
# policies and administers claims (proxy, reopen).
_EPIC27_ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "finance": ("expense:read", "expense:approve"),
    "finance_admin": ("expense:read", "expense:approve", "expense:admin", "expense:policy"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC27_ROLE_GRANTS[r.key])})
    if r.key in _EPIC27_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 26 (definitions only, AD-16): Finance reads leave and holidays.
_EPIC26_ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "finance": ("leave:read", "holiday:read"),
    "finance_admin": ("leave:read", "holiday:read", "leave:read_team"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC26_ROLE_GRANTS[r.key])})
    if r.key in _EPIC26_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 29 (definitions only, AD-16): Finance prepares drafts and runs; Finance Admin also configures, raises notes
# and handles e-invoices (``invoice:issue`` is already on Finance Admin).
_EPIC29_ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "finance": ("billing:read", "billing:write"),
    "finance_admin": (
        "billing:read",
        "billing:write",
        "billing:configure",
        "credit_note:write",
        "einvoice:write",
    ),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC29_ROLE_GRANTS[r.key])})
    if r.key in _EPIC29_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 34 (definitions only, AD-16): Finance Admin exports finance lists with amounts.
_EPIC34_ROLE_GRANTS: dict[str, tuple[str, ...]] = {"finance_admin": ("report:finance_export",)}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC34_ROLE_GRANTS[r.key])})
    if r.key in _EPIC34_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 28 (definitions only, AD-16): Finance Admin configures payroll and may reopen pay periods.
_EPIC28_ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "finance_admin": ("payroll:configure", "period:reopen_pay")
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC28_ROLE_GRANTS[r.key])})
    if r.key in _EPIC28_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 30 (definitions only, AD-16): Finance works receivables; Finance Admin also reverses, writes off and reopens.
_EPIC30_ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "finance": (
        "receivables:read",
        "collections:write",
        "dispute:write",
        "tds:write",
        "accounting:export",
        "reconciliation:read",
    ),
    "finance_admin": (
        "receivables:read",
        "payment:reverse",
        "collections:write",
        "dispute:write",
        "tds:write",
        "period:reopen",
        "accounting:export",
        "reconciliation:read",
    ),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC30_ROLE_GRANTS[r.key])})
    if r.key in _EPIC30_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)


# Phase close (``20261006_900_merge_phase2``, AD-16): the Phase 2 grants the domain specs deferred to "the
# phase-close resync" -- for the frozen P1 roles and the FR-81 gaps of the Phase 2 roles. Data-scoped keys keep
# the role's usual scope; administrative (tenant-only) keys are granted at tenant scope. The merge migration
# freezes the resulting ``DEFAULT_ROLES`` and re-syncs every tenant's SYSTEM non-admin roles to it.
def _phase_close(scope: Scope, *perms: str) -> dict[str, Scope]:
    return {p: (scope if scope in CATALOG[p].scopes else _T) for p in _grant(scope, *perms)}


_DOCS_RW = ("document:read", "document:write")
_PHASE_CLOSE_GRANTS: dict[str, dict[str, Scope]] = {
    "recruitment_manager": {
        **_phase_close(_B, *_DOCS_RW, "signature_request:create", "placement:read"),
        **_phase_close(_T, "vendor:release", "vendor_submission:review"),
    },
    "delivery_manager": {
        **_phase_close(_B, *_DOCS_RW, "signature_request:create", "placement:read"),
        **_phase_close(_B, "bench:read", "bench:review_matches", "bench:write", "bench:edit_cost"),
        **_phase_close(_T, "vendor:performance"),
    },
    "account_manager": {
        **_phase_close(
            _M, "document:read", "placement:read", "compliance:read", "bench:read", "bench:review_matches"
        ),
        **_phase_close(
            _M, "leave:read", "leave:approve", "leave:read_team", "timesheet:read", "timesheet:approve"
        ),
        **_phase_close(_M, "expense:read", "expense:approve"),
        **_phase_close(_O, "document:write", "placement:create", "placement:update", "placement:rates"),
        **_phase_close(_O, "messaging:send", "extension:write"),
        **_phase_close(
            _T, "bench:write", "bench:edit_cost", "onboarding:read", "holiday:read", "dispute:write"
        ),
        **_phase_close(_T, "client_portal:manage", "contract:read", "rate_card:read", "po:read"),
    },
    "recruiter": {
        **_phase_close(_M, *_DOCS_RW, "placement:read", "bench:read", "bench:review_matches"),
        **_phase_close(_O, "messaging:send"),
        **_phase_close(
            _T, "bgv:read", "reference_check:request", "vendor_submission:review", "onboarding:read"
        ),
    },
    "sales": _phase_close(_T, "contract:read", "rate_card:read"),
    "hr_onboarding": _phase_close(
        _T,
        *_DOCS_RW,
        "document:hr",
        "document_template:manage",
        "signature_request:create",
        "messaging:send",
        "placement:read",
        "placement:join",
        "placement:drop",
        "worker:read",
        "worker_bank:approve",
        "offboarding:write",
        "offboarding:close",
    ),
    "compliance": _phase_close(_T, "document:read", "document:hr"),
    "finance": _phase_close(_T, "document:read", "document:finance", "placement:read"),
    "finance_admin": _phase_close(_T, "document:read", "document:finance", "placement:read"),
    "vendor_manager": _phase_close(_T, "vendor_submission:review"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_PHASE_CLOSE_GRANTS[r.key]})
    if r.key in _PHASE_CLOSE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 50 (definitions only, AD-16; no resync): the Account Manager configures their Clients' SSO.
_EPIC50_ROLE_GRANTS: dict[str, tuple[str, ...]] = {"account_manager": ("client_sso:manage",)}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC50_ROLE_GRANTS[r.key])})
    if r.key in _EPIC50_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 37 (definitions only, AD-16; no resync -- the Phase 3 close resyncs): Recruitment Manager and Delivery
# Manager verify skills and read the insight at their usual (BU) scope; Recruiter verifies at team scope (as for
# ``candidate:update``). The Tenant Admin holds every key dynamically.
_EPIC37_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "recruitment_manager": _grant(_B, "skill:verify", "skills_insight:read"),
    "delivery_manager": _grant(_B, "skill:verify", "skills_insight:read"),
    "recruiter": _grant(_M, "skill:verify"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC37_ROLE_GRANTS[r.key]})
    if r.key in _EPIC37_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 51 (definitions only, AD-16; the Phase 3 close resync applies them): Finance Admin gets all three keys,
# Finance gets ``tds_recon:write``.
_EPIC51_ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "finance": ("tds_recon:write",),
    "finance_admin": ("dsc:configure", "tds_recon:write", "fx_feed:configure"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC51_ROLE_GRANTS[r.key])})
    if r.key in _EPIC51_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 46 (definitions only, AD-16; no resync -- the Phase 3 close resyncs): the new Immigration Coordinator default
# Role (build plan 4) and ``immigration:read`` for Account Managers at BU scope. ``identity:view`` is sensitive and
# never granted by default. ``document:hr`` lets the Coordinator open the Case documents (stored at access level hr).
DEFAULT_ROLES += (
    RoleTemplate(
        "immigration_coordinator",
        "Immigration Coordinator",
        "Runs visa and work-permit cases: checklists, counsel, outcomes and permits.",
        {
            **_grant(_T, "immigration:read", "immigration:manage", "immigration:configure"),
            **_grant(
                _T, "candidate:read", "placement:read", "document:read", "document:write", "document:hr"
            ),
            **_grant(_T, "credential:write", "compliance:read", "note:write", *_DIRECTORY_READ),
            **_grant(_O, *_OWN_TASKS),
        },
    ),
)
_EPIC46_ROLE_GRANTS: dict[str, dict[str, Scope]] = {"account_manager": _grant(_B, "immigration:read")}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC46_ROLE_GRANTS[r.key]})
    if r.key in _EPIC46_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 45 (definitions only, AD-16; no resync -- the Phase 3 close resyncs): Delivery Manager runs projects (BU
# scope) and records offline acceptances; Account Manager (team) and Finance / Finance Admin read projects.
_EPIC45_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "delivery_manager": {**_grant(_B, "project:read", "project:write"), **_grant(_T, "deliverable:record")},
    "account_manager": _grant(_M, "project:read"),
    "finance": _grant(_T, "project:read"),
    "finance_admin": _grant(_T, "project:read"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC45_ROLE_GRANTS[r.key]})
    if r.key in _EPIC45_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 47 (definitions only, AD-16; no resync -- the Phase 3 close resyncs): Recruitment Manager, Account Manager
# and Finance build and schedule custom reports.
_EPIC47_ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "recruitment_manager": ("report:build", "report:schedule"),
    "account_manager": ("report:build", "report:schedule"),
    "finance": ("report:build", "report:schedule"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC47_ROLE_GRANTS[r.key])})
    if r.key in _EPIC47_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 38 (definitions only, AD-16; no resync -- the Phase 3 close resyncs): Recruiter and Recruitment Manager
# build segments and pools; Recruitment Manager also manages tenant pools and templates.
_EPIC38_ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "recruiter": ("pool:read", "pool:write", "segment:write"),
    "recruitment_manager": ("pool:read", "pool:write", "segment:write", "pool:manage"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC38_ROLE_GRANTS[r.key])})
    if r.key in _EPIC38_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 42 (definitions only, AD-16; no resync -- the Phase 3 close resyncs): Recruiters read referrals (team),
# Recruitment Managers read and manage them (BU), Finance approves rewards; the Tenant Admin configures (dynamic).
_EPIC42_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "recruiter": _grant(_M, "referral:read"),
    "recruitment_manager": _grant(_B, "referral:read", "referral:manage"),
    "finance": _grant(_T, "referral_reward:approve"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC42_ROLE_GRANTS[r.key]})
    if r.key in _EPIC42_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 40 (definitions only, AD-16; no resync -- the Phase 3 close resyncs): Recruiter reads, invites and reviews;
# Recruitment Manager also authors. The Tenant Admin holds every key (incl. ``assessment:configure``) dynamically.
_EPIC40_ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "recruiter": ("assessment:read", "assessment:invite", "assessment:review"),
    "recruitment_manager": ("assessment:read", "assessment:invite", "assessment:review", "assessment:author"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC40_ROLE_GRANTS[r.key])})
    if r.key in _EPIC40_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 48 (definitions only, AD-16; no resync -- the Phase 3 close resyncs): Recruitment Managers read predictions,
# Account Managers also read attrition risk (FR-243); the Tenant Admin holds all three keys dynamically.
_EPIC48_ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "recruitment_manager": ("prediction:read",),
    "account_manager": ("prediction:read", "prediction:read_attrition"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC48_ROLE_GRANTS[r.key])})
    if r.key in _EPIC48_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 49 (definitions only, AD-16; no resync -- the Phase 3 close resyncs): Compliance holds the three fairness keys
# at tenant scope; the Tenant Admin holds them dynamically.
_EPIC49_ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "compliance": ("fairness:read", "fairness:configure", "aedt:manage")
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC49_ROLE_GRANTS[r.key])})
    if r.key in _EPIC49_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 43 (definitions only, AD-16; no resync -- the Phase 3 close resyncs): the new Campus Recruiter default Role
# (build plan 4) and the campus keys for Recruitment Managers at BU scope. No ``compensation:view``, no
# ``offer:approve`` / ``offer:release`` for the Campus Recruiter.
DEFAULT_ROLES += (
    RoleTemplate(
        "campus_recruiter",
        "Campus Recruiter",
        "Runs campus drives and recruitment events.",
        {
            **_grant(_T, "campus:read", "campus:manage", "event:manage", "campus_eligibility:override"),
            **_grant(
                _T, "candidate:read", "candidate:create", "client:read", "contact:read", "email_template:read"
            ),
            **_grant(_T, *_DIRECTORY_READ, "assessment:read", "assessment:invite", "pool:read"),
            **_grant(
                _M,
                "demand:read",
                "application:read",
                "application:create",
                "application:move",
                "application:reject",
            ),
            **_grant(_M, "interview:read", "interview:create", "interview:schedule", "interview:outcome"),
            **_grant(_M, "scorecard:read", "scorecard:submit"),
            **_grant(_M, "offer:read", "offer:create", "offer:update", "offer:withdraw"),
            **_grant(_O, *_OWN_TASKS, "email:send", "messaging:send", "report:read"),
        },
    ),
)
_EPIC43_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "recruitment_manager": _grant(
        _B, "campus:read", "campus:manage", "event:manage", "campus_eligibility:override"
    )
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC43_ROLE_GRANTS[r.key]})
    if r.key in _EPIC43_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 41 (definitions only, AD-16; no resync -- the Phase 3 close resyncs): Recruitment Manager both keys (BU),
# Recruiter ``mobility:manage`` (team), HR / Onboarding ``mobility:transfer`` (tenant).
_EPIC41_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "recruitment_manager": _grant(_B, "mobility:manage", "mobility:transfer"),
    "recruiter": _grant(_M, "mobility:manage"),
    "hr_onboarding": _grant(_T, "mobility:transfer"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC41_ROLE_GRANTS[r.key]})
    if r.key in _EPIC41_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 39 (definitions only, AD-16; no resync -- the Phase 3 close resyncs): the new Recruitment Marketer default Role
# (build plan 4); Recruitment Managers read and approve campaigns; Recruiters read campaigns and enrol into sequences.
DEFAULT_ROLES += (
    RoleTemplate(
        "recruitment_marketer",
        "Recruitment Marketer",
        "Builds audiences, campaigns and nurture sequences.",
        {
            **_grant(_T, "campaign:read", "campaign:write", "campaign:send", "sequence:write", "pool:read"),
            **_grant(
                _T, "candidate:read", "email_template:read", "report:read", "client:read", "demand:read"
            ),
            **_grant(_T, "note:write", *_DIRECTORY_READ),
            **_grant(_O, *_OWN_TASKS),
        },
    ),
)
_EPIC39_ROLE_GRANTS: dict[str, tuple[str, ...]] = {
    "recruitment_manager": ("campaign:read", "campaign:approve"),
    "recruiter": ("campaign:read", "sequence:write"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_grant(_T, *_EPIC39_ROLE_GRANTS[r.key])})
    if r.key in _EPIC39_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 44 (definitions only, AD-16; no resync -- the Phase 3 close resyncs): Recruitment and Delivery Managers plan,
# budget and approve at BU scope; Account Managers read and write their own plans (they receive Client forecasts);
# Finance Admin reads every plan.
_EPIC44_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "recruitment_manager": _grant(
        _B, "workforce_plan:read", "workforce_plan:write", "workforce_plan:approve"
    ),
    "delivery_manager": _grant(_B, "workforce_plan:read", "workforce_plan:write", "workforce_plan:approve"),
    "account_manager": _grant(_O, "workforce_plan:read", "workforce_plan:write"),
    "finance_admin": _grant(_T, "workforce_plan:read"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC44_ROLE_GRANTS[r.key]})
    if r.key in _EPIC44_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 53 (definitions only, AD-16; no resync -- the Phase 4 close resyncs, FR-328): Proposal decisions and the agent
# message keys. The Tenant Admin holds every key dynamically; the AI Administrator role is Epic 65's.
_DECIDE_MSG = ("proposal:decide", "message:draft", "message:send_templated")
_EPIC53_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "recruiter": {**_grant(_M, "proposal:decide"), **_grant(_O, "message:draft", "message:send_templated")},
    "recruitment_manager": _grant(_B, *_DECIDE_MSG),
    "delivery_manager": _grant(_B, *_DECIDE_MSG),
    "account_manager": _grant(_O, *_DECIDE_MSG),
    # Coordinator addition (Epic 63 dependency): finance roles may let agents send approved templates.
    "finance": _grant(_T, *_DECIDE_MSG),
    "finance_admin": _grant(_T, *_DECIDE_MSG),
    "hr_onboarding": _grant(_T, *_DECIDE_MSG, "knowledge:read"),
    "compliance": _grant(_T, *_DECIDE_MSG, "knowledge:read"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC53_ROLE_GRANTS[r.key]})
    if r.key in _EPIC53_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 54 (definitions only; the Phase 4 close resyncs): the Copilot panel for the working roles. Not for
# interviewer, viewer or grievance_officer; the Tenant Admin holds it dynamically.
_EPIC54_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    key: _grant(_T, "copilot:use")
    for key in (
        "recruiter", "recruitment_manager", "delivery_manager", "account_manager", "sales", "campus_recruiter",
        "recruitment_marketer", "vendor_manager", "hr_onboarding", "compliance", "immigration_coordinator", "finance",
        "finance_admin",
    )
}  # fmt: skip
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC54_ROLE_GRANTS[r.key]})
    if r.key in _EPIC54_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 57 (definitions only; the Phase 4 close resyncs): AI screening for the recruiting roles.
_EPIC57_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "recruiter": _grant(_M, "screening:conduct"),
    "recruitment_manager": _grant(_B, "screening:conduct"),
    "delivery_manager": _grant(_B, "screening:conduct"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC57_ROLE_GRANTS[r.key]})
    if r.key in _EPIC57_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 58 (definitions only; the Phase 4 close resyncs): voice screening requests and call uploads.
_EPIC58_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "recruiter": _grant(_M, "voice:upload"),
    "recruitment_manager": _grant(_B, "voice:upload"),
    "delivery_manager": _grant(_B, "voice:upload"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC58_ROLE_GRANTS[r.key]})
    if r.key in _EPIC58_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 59 (definitions only; the Phase 4 close resyncs): free/busy for slot proposals.
_EPIC59_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "recruiter": _grant(_M, "calendar:free_busy"),
    "recruitment_manager": _grant(_B, "calendar:free_busy"),
    "delivery_manager": _grant(_B, "calendar:free_busy"),
    "interviewer": _grant(_O, "calendar:free_busy"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC59_ROLE_GRANTS[r.key]})
    if r.key in _EPIC59_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 60 (definitions only; the Phase 4 close resyncs): interview capture and the Interview Assistant.
_EPIC60_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "interviewer": _grant(_O, "interview_capture:manage"),
    "recruiter": _grant(_M, "interview_capture:manage"),
    "recruitment_manager": _grant(_B, "interview_capture:manage"),
    "delivery_manager": _grant(_B, "interview_capture:manage"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC60_ROLE_GRANTS[r.key]})
    if r.key in _EPIC60_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 61 (definitions only; the Phase 4 close resyncs): agents may prepare draft submission packs.
_EPIC61_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "recruiter": _grant(_M, "submission:draft"),
    "recruitment_manager": _grant(_B, "submission:draft"),
    "delivery_manager": _grant(_B, "submission:draft"),
    "account_manager": _grant(_M, "submission:draft"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC61_ROLE_GRANTS[r.key]})
    if r.key in _EPIC61_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 62 (definitions only; the Phase 4 close resyncs): Knowledge Sources and BGV provider follow-ups.
_EPIC62_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "hr_onboarding": _grant(_T, "knowledge:manage"),
    "compliance": _grant(_T, "knowledge:manage", "bgv:follow_up"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC62_ROLE_GRANTS[r.key]})
    if r.key in _EPIC62_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 65 (definitions only; the Phase 4 close merge resyncs, FR-328): the AI Administrator system role -- AI
# settings, agent configuration, budgets, kill switch, governance console and evaluation results. No business-data
# permissions (no candidate:read, report:read, proposal:decide, copilot:use) and no ``ai:payload_read`` (C-14).
DEFAULT_ROLES += (
    RoleTemplate(
        "ai_administrator",
        "AI Administrator",
        "AI settings, agent configuration, budgets, kill switch, governance console and evaluation results; "
        "no business data beyond run metadata.",
        {
            **_grant(_T, "ai:govern", "ai:agent_configure", "ai:kill_switch", "role:read", *_DIRECTORY_READ),
            **_grant(_O, *_OWN_TASKS),
        },
    ),
)
_EPIC65_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    # Campus recruiters own the campus pipelines the Scheduling and Sourcing agents propose on.
    "campus_recruiter": _grant(_M, "proposal:decide"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC65_ROLE_GRANTS[r.key]})
    if r.key in _EPIC65_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 63 (definitions only; the Phase 4 close resyncs): agents may prepare invoice runs for the finance roles. The
# finance roles' ``message:send_templated`` (C-6) is already in ``_EPIC53_ROLE_GRANTS``.
_EPIC63_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "finance": _grant(_T, "billing:prepare", "message:send_templated"),
    "finance_admin": _grant(_T, "billing:prepare", "message:send_templated"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC63_ROLE_GRANTS[r.key]})
    if r.key in _EPIC63_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 66 (definitions only; the Phase 5 close resyncs): managers and finance admins see the Installations. The
# Integration Administrator system role is Epic 79's.
_EPIC66_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "recruitment_manager": _grant(_T, "integration:view"),
    "delivery_manager": _grant(_T, "integration:view"),
    "finance_admin": _grant(_T, "integration:view"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC66_ROLE_GRANTS[r.key]})
    if r.key in _EPIC66_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 76 (definitions only; the Phase 5 close resyncs): recruiters place screening calls.
_EPIC76_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "recruiter": _grant(_M, "voice:call"),
    "recruitment_manager": _grant(_B, "voice:call"),
    "delivery_manager": _grant(_B, "voice:call"),
    "campus_recruiter": _grant(_M, "voice:call"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC76_ROLE_GRANTS[r.key]})
    if r.key in _EPIC76_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 78 (definitions only; the Phase 5 close resyncs): marketers and managers approve AI-drafted campaign copy.
_EPIC78_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "recruitment_marketer": _grant(_T, "campaign:copy_approve"),
    "recruitment_manager": _grant(_B, "campaign:copy_approve"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC78_ROLE_GRANTS[r.key]})
    if r.key in _EPIC78_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 70 (definitions only; the Phase 5 close resyncs): recruiters and managers publish to external job boards.
_EPIC70_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "recruiter": _grant(_M, "posting:publish_external"),
    "recruitment_manager": _grant(_B, "posting:publish_external"),
    "account_manager": _grant(_M, "posting:publish_external"),
    "delivery_manager": _grant(_B, "posting:publish_external"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC70_ROLE_GRANTS[r.key]})
    if r.key in _EPIC70_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 73 (definitions only; the Phase 5 close resyncs): business users may connect an external AI assistant.
_EPIC73_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    key: _grant(_T, "mcp:use")
    for key in (
        "recruiter", "recruitment_manager", "delivery_manager", "account_manager", "campus_recruiter", "sales",
    )
}  # fmt: skip
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC73_ROLE_GRANTS[r.key]})
    if r.key in _EPIC73_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 71 (definitions only; the Phase 5 close resyncs): HR / onboarding and delivery push to the HRMS; Finance syncs
# the ledger.
_EPIC71_ROLE_GRANTS: dict[str, dict[str, Scope]] = {
    "hr_onboarding": _grant(_B, "hrms:sync"),
    "delivery_manager": _grant(_B, "hrms:sync"),
    "finance_admin": _grant(_T, "ledger:sync"),
    "finance": _grant(_B, "ledger:sync"),
}
DEFAULT_ROLES = tuple(
    dataclasses.replace(r, permissions={**r.permissions, **_EPIC71_ROLE_GRANTS[r.key]})
    if r.key in _EPIC71_ROLE_GRANTS
    else r
    for r in DEFAULT_ROLES
)

# Epic 79 (phase close, FR-412): the Integration Administrator system role -- Marketplace, Installations, webhooks, the
# ecosystem console and the Ecosystem Kill Switch, with directory reads and own fix-it Tasks only. No business-data
# permissions (no candidate:read, report:read, invoice:*, mcp:use, voice:call). The Phase 5 close (``*_900_merge_phase5``)
# resyncs it into every tenant.
DEFAULT_ROLES += (
    RoleTemplate(
        "integration_administrator",
        "Integration Administrator",
        "Marketplace, Installations, webhooks, the ecosystem console and the Ecosystem Kill Switch; "
        "no business-data permissions.",
        {
            **_grant(
                _T, "integration:view", "integration:manage", "ecosystem:kill_switch", "usage:read",
                "webhook:manage", "app:develop", "role:read", "user:read", *_DIRECTORY_READ,
            ),
            **_grant(_O, *_OWN_TASKS),
        },
    ),
)  # fmt: skip
# Epic 79 adds no grants to existing roles (68's ``webhook:manage`` lives on the Integration Administrator above); the
# dict exists for symmetry with the other ``_EPIC*_ROLE_GRANTS`` and the permission-inventory test.
_EPIC79_ROLE_GRANTS: dict[str, dict[str, Scope]] = {}

DEFAULT_ROLE_KEYS: frozenset[str] = frozenset(r.key for r in DEFAULT_ROLES)


def effective_role_grants(key: str, is_system: bool, stored: Mapping[str, str]) -> Mapping[str, str]:
    """Grants a role confers. The system Tenant Admin always holds every *current* catalog permission at
    tenant scope, whatever JSON was stored when it was seeded (the catalog grows over time)."""
    if is_system and key == TENANT_ADMIN:
        return dict.fromkeys(CATALOG, Scope.TENANT.value)
    return stored


# ================================================================ principal
@dataclasses.dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller, stored on the request context (``update_context(principal=...)``).

    User principals also carry their resolved access: role ids, ``permissions`` (permission -> widest
    scope), team ids and business unit. Other principal types have empty access.
    """

    type: PrincipalType
    id: uuid.UUID
    session_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    role_ids: frozenset[uuid.UUID] = frozenset()
    permissions: Mapping[str, Scope] = dataclasses.field(default_factory=dict)
    team_ids: frozenset[uuid.UUID] = frozenset()
    business_unit_id: uuid.UUID | None = None
    # Epic 12: read-only principals (support access, offboarding tenants) and their grant / key ids.
    read_only: bool = False
    support_grant_id: uuid.UUID | None = None
    api_key_id: uuid.UUID | None = None
    # Epic 13 (AD-26): external principals carry their organization (client / vendor id) and portal keys.
    org_id: uuid.UUID | None = None
    portal_permissions: frozenset[str] = frozenset()
    # Epic 66 (AD-75): App Principals (``type="app"``, ``id`` = the Installation). ``acting_user_id`` is the user of a
    # delegated (authorization-code) token; ``oauth_scopes`` the effective scopes; ``resource`` ``api`` | ``mcp``.
    app_id: uuid.UUID | None = None
    app_installation_id: uuid.UUID | None = None
    oauth_token_id: uuid.UUID | None = None
    acting_user_id: uuid.UUID | None = None
    oauth_scopes: frozenset[str] = frozenset()
    resource: str | None = None

    def scope_of(self, permission: str) -> Scope | None:
        return self.permissions.get(permission)

    def has(self, permission: str, at_least: Scope = Scope.OWN) -> bool:
        scope = self.permissions.get(permission)
        return scope is not None and scope.covers(at_least)


def forbidden(permission: str) -> Forbidden:
    """403 with the FR-82 details: ``action`` (the permission's label), ``required_permission`` (and the
    legacy ``permission``); ``ask`` is derived from the principal by the error handler."""
    definition = CATALOG.get(permission)
    return Forbidden(
        "You do not have permission to perform this action",
        details={"permission": permission},
        action=definition.description if definition is not None else permission,
        required_permission=permission,
    )


def check(principal: Principal, permission: str, at_least: Scope = Scope.OWN) -> None:
    """Raise 403 ``forbidden`` (``details.permission``) unless ``principal`` holds ``permission``."""
    if permission not in CATALOG:
        msg = f"unknown permission {permission!r}"
        raise ValueError(msg)
    if not principal.has(permission, at_least):
        raise forbidden(permission)


# ---------------------------------------------------------------- FastAPI dependency
PrincipalResolver = Callable[[Request], Awaitable[Principal]]
_resolver: PrincipalResolver | None = None


def set_principal_resolver(resolver: PrincipalResolver) -> None:
    """Register the authentication dependency (``identity.dependencies.current_user``)."""
    global _resolver  # process-wide wiring, set once at import
    _resolver = resolver


async def current_principal(request: Request) -> Principal:
    if _resolver is None:  # pragma: no cover - wiring error
        msg = "no principal resolver registered (import app.dependencies.auth)"
        raise RuntimeError(msg)
    return await _resolver(request)


def require(permission: str, at_least: Scope = Scope.OWN) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory: authenticate the user and require ``permission`` (at ``at_least`` scope).

    ``principal: Annotated[Principal, Depends(require("user:read"))]``.
    """
    if permission not in CATALOG:
        msg = f"unknown permission {permission!r}"
        raise ValueError(msg)

    async def dependency(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if principal.type in EXTERNAL_PRINCIPAL_TYPES or principal.type == "agent":  # AD-26 / AD-60: never routed
            raise forbidden(permission)
        check(principal, permission, at_least)
        return principal

    dependency.__name__ = f"require_{permission.replace(':', '_')}"
    return dependency


def require_any(*permissions: str, at_least: Scope = Scope.OWN) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory (Epic 16): authenticate the user and require **any one** of ``permissions``."""
    for permission in permissions:
        if permission not in CATALOG:
            msg = f"unknown permission {permission!r}"
            raise ValueError(msg)

    async def dependency(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if principal.type in EXTERNAL_PRINCIPAL_TYPES or principal.type == "agent":
            raise forbidden(permissions[0])
        if not any(principal.has(p, at_least) for p in permissions):
            raise forbidden(permissions[0])
        return principal

    dependency.__name__ = "require_any_" + "_".join(p.replace(":", "_") for p in permissions)
    return dependency


# ================================================================ row scoping
_team_members = table("team_members", column("team_id"), column("user_id"))
_users = table("users", column("id"), column("business_unit_id"))


def scope_condition(
    principal: Principal, permission: str, owner_col: Column_, bu_col: Column_ | None = None
) -> ColumnElement[bool] | None:
    """Return the SQL condition for rows ``principal`` may see under ``permission`` (None = all).

    * ``own``: ``owner_col`` is the principal;
    * ``team``: owned by the principal or by any member of the principal's teams;
    * ``bu``: team rows plus rows of the principal's business unit -- ``bu_col = my BU`` when the table
      has a BU column, else rows owned by users of my BU. Without a BU it degrades to ``team``;
    * ``tenant``: no restriction (RLS still confines everything to the tenant);
    * permission not held: matches nothing.
    """
    scope = principal.permissions.get(permission)
    if scope is None:
        return false()
    if scope is Scope.TENANT:
        return None
    # Epic 67: a delegated App Principal (``id`` = the Installation) owns rows as its acting user (data scope = the user's).
    me = principal.acting_user_id if principal.type == "app" and principal.acting_user_id is not None else principal.id
    conditions: list[ColumnElement[bool]] = [owner_col == me]
    if scope.covers(Scope.TEAM) and principal.team_ids:
        members = select(_team_members.c.user_id).where(_team_members.c.team_id.in_(principal.team_ids))
        conditions.append(owner_col.in_(members))
    if scope.covers(Scope.BU) and principal.business_unit_id is not None:
        if bu_col is not None:
            conditions.append(bu_col == principal.business_unit_id)
        else:
            bu_users = select(_users.c.id).where(_users.c.business_unit_id == principal.business_unit_id)
            conditions.append(owner_col.in_(bu_users))
    return or_(*conditions) if len(conditions) > 1 else conditions[0]


def scope_filter[S: Select[Any]](
    stmt: S, principal: Principal, permission: str, owner_col: Column_, bu_col: Column_ | None = None
) -> S:
    """Narrow ``stmt`` to the rows ``principal`` may see under ``permission`` (see ``scope_condition``)."""
    condition = scope_condition(principal, permission, owner_col, bu_col)
    return stmt if condition is None else stmt.where(condition)


# ================================================================ compensation masking
# Field names that carry compensation, rate or margin values (Phase 1 entities use these names).
COMPENSATION_FIELDS: frozenset[str] = frozenset(
    {
        "compensation",
        "salary",
        "ctc",
        "bonus",
        "rate",
        "bill_rate",
        "pay_rate",
        "budget",
        "budget_min",
        "budget_max",
        "margin",
    }
)
# Any key containing one of these (``expected_ctc``, ``min_salary``, ``margin_percent``, ...) is masked too;
# ``min_``/``max_`` variants of every exact name above are covered as well (``min_rate``, ``max_budget``).
_COMPENSATION_RE = re.compile(r"salary|compensation|ctc|pay_rate|bill_rate|margin|bonus")
_SCALARS = (str, int, float, bool, Decimal, datetime, date, time, uuid.UUID, enum.Enum, type(None))


def is_compensation_field(key: object, fields: frozenset[str] = COMPENSATION_FIELDS) -> bool:
    if not isinstance(key, str):
        return False
    name = key.lower()
    base = name.removeprefix("min_").removeprefix("max_")
    return name in fields or base in fields or _COMPENSATION_RE.search(name) is not None


def can_view_compensation(principal: Principal | None) -> bool:
    return principal is not None and COMPENSATION_VIEW in principal.permissions


def mask_compensation(
    data: Any, principal: Principal | None, fields: frozenset[str] = COMPENSATION_FIELDS
) -> Any:
    """Return ``data`` with every compensation field set to None unless the principal may see it.

    Accepts pydantic models (dumped), mappings and lists/tuples, recursively; nested values must be those
    or plain scalars. Anything else (e.g. an ORM object) raises ``TypeError`` rather than leaking unmasked.
    The input is never mutated.
    """
    if not isinstance(data, BaseModel | Mapping | list | tuple):
        msg = f"mask_compensation cannot mask {type(data).__name__}"
        raise TypeError(msg)
    if can_view_compensation(principal):
        return data.model_dump() if isinstance(data, BaseModel) else data
    return _mask(data, fields)


def _mask(data: Any, fields: frozenset[str]) -> Any:
    if isinstance(data, BaseModel):
        return _mask(data.model_dump(), fields)
    if isinstance(data, Mapping):
        return {k: (None if is_compensation_field(k, fields) else _mask(v, fields)) for k, v in data.items()}
    if isinstance(data, list | tuple):
        return [_mask(v, fields) for v in data]
    if isinstance(data, _SCALARS):
        return data
    msg = f"mask_compensation cannot mask {type(data).__name__}"
    raise TypeError(msg)
