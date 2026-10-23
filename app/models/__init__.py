"""SQLAlchemy models, one module per domain (AD-1, layered layout).

Importing this package registers every table on ``Base.metadata`` (Alembic ``env.py`` and the app import
it): core tables (``app.core.sequences``, ``app.core.audit``, ``app.core.secure_links``), identity (tenants,
users, roles, sessions) and every other domain.
"""

import app.core.audit  # ruff: ignore[unused-import]  (side-effect import: registers tables on Base.metadata)
import app.core.secure_links  # ruff: ignore[unused-import]  (side-effect import: registers tables on Base.metadata)
import app.core.sequences  # ruff: ignore[unused-import]
from app.models import activity, ai, candidates, clients, files, identity

__all__ = ["activity", "ai", "candidates", "clients", "files", "identity"]

# Epic 12: privacy, platform operations, API keys, tenant settings hub.
from app.models import api_keys, platform, privacy, settings

__all__ += ["api_keys", "platform", "privacy", "settings"]

# Epic 4: generic approvals, demands.
from app.models import approvals, demands  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["approvals", "demands"]

# Epic 5: search projection, embeddings (AI service), matching.
from app.models import embeddings, matching, search  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["embeddings", "matching", "search"]

# Epic 10: communication (emails, templates), tasks, notifications, workflow rules.
from app.models import comms, notifications, tasks, workflow  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["comms", "notifications", "tasks", "workflow"]

# Epic 6: pipeline (stages, applications, screenings, submissions, client feedback).
from app.models import pipeline  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["pipeline"]

# Epic 7: interviews (plans, kits, interviews, scorecards, outcomes) and calendar / mailbox connections.
from app.models import connections, interviews  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["connections", "interviews"]

# Epic 8: offers (settings, letter templates, offers, versions, status history, candidate responses).
from app.models import offers  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["offers"]

# Epic 11: reporting (exports, user active days).
from app.models import reporting  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["reporting"]

# Epic 9: career portal & candidate portal (career sites, apply sessions, answers, data exports).
from app.models import portal  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["portal"]

# Epic 13 (Phase 2 foundation & contract layer): reference data, legal entities, external principals, generic
# SLA clocks, posting ledgers, idempotency, file imports, connectors and the contract hub tables.
from app.models import (  # ruff: ignore[module-import-not-at-top-of-file]
    billing,
    commercials,
    connectors,
    external_auth,
    idempotency,
    imports,
    legal_entities,
    placements,
    postings,
    reference,
    sla,
    vendors,
)

__all__ += [
    "billing",
    "commercials",
    "connectors",
    "external_auth",
    "idempotency",
    "imports",
    "legal_entities",
    "placements",
    "postings",
    "reference",
    "sla",
    "vendors",
]

# Epic 14 (Phase 2 access): single sign-on for internal Users.
from app.models import sso  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["sso"]

# Epic 22: documents, templates and eSignature.
from app.models import documents  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["documents"]

# Epic 17: vendor payable statements (payroll sub-domain; the vendor tables live in ``vendors``).
from app.models import vendor_statements  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["vendor_statements"]

# Epic 15: sales CRM (leads, opportunities, record links).
from app.models import crm  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["crm"]

# Epic 21: compliance requirements, credentials and the per-placement checklist.
from app.models import compliance  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["compliance"]

# Epic 23: digital onboarding (templates, cases, tasks, policy acknowledgements).
from app.models import onboarding  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["onboarding"]

# Epic 25: timesheets (configs, timesheets, entries, adjustments, rate adjustments); Epic 26 appends leave.
from app.models import time  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["time"]

# Epic 20: background verification (packages, cases, checks, discrepancies) and reference checks.
from app.models import bgv  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["bgv"]

# Epic 27: expenses (settings, versioned policies, claims, lines, line decisions).
from app.models import expenses  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["expenses"]

# Epic 31: extension, conversion & offboarding (settings, end alerts, extensions, offboarding cases, conversions).
from app.models import placements_exit  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["placements_exit"]

# Epic 30: receivables, collections & period close (payments, allocations, credits, TDS, write-offs, disputes, ...).
from app.models import receivables  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["receivables"]

# Epic 28: payroll export (settings, pay calendars / periods, export versions / rows / items, mappings).
from app.models import payroll  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["payroll"]

# Epic 18: client portal (slot proposals, client user delegations, compliance packs, download log).
from app.models import client_portal  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["client_portal"]

# Epic 35: go-live import rows (on the foundation ``file_import_batches``).
from app.models import golive  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["golive"]

# Epic 35: External-Principal portal notification preferences.
from app.models import portal_notifications  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["portal_notifications"]

# Epic 36 (Phase 3 foundation & contract layer, AD-39): hub tables owned by Epics 38, 40, 44 and 45.
from app.models import (  # ruff: ignore[module-import-not-at-top-of-file]
    assessments,
    projects,
    talent_pools,
    workforce,
)

__all__ += ["assessments", "projects", "talent_pools", "workforce"]

# Epic 37 (skills graph & proficiency, AD-41): platform / tenant edges, settings, evidence, suggestions.
from app.models import skills_graph  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["skills_graph"]

# Epic 46 (immigration, AD-52): visa type templates / types / versions, counsel, cases, events, dependants,
# document requests, permits.
from app.models import immigration  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["immigration"]

# Epic 47 (custom report builder & scheduled reports): definitions, versions, shares, pins, schedules, runs, settings.
from app.models import report_builder  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["report_builder"]

# Epic 42 (employee referrals, AD-50): settings, programs, referrals, share links, rewards.
from app.models import referrals  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["referrals"]

# Epic 48 (predictive analytics, AD-45): settings, feature specs, training sets, models, predictions.
from app.models import analytics  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["analytics"]

# Epic 39 (recruitment marketing, AD-43): settings, audiences, snapshots, campaigns, messages, engagement,
# attributions, sequences, enrolments, workflow trigger caps.
from app.models import campaigns  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["campaigns"]

# Epic 43 (campus hiring & recruitment events): institutions, placement officers, drives, registrations, interview
# batches, recruitment events, check-ins, stations and queue entries.
from app.models import campus  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["campus"]

# Epic 41 (internal mobility, AD-50): settings, profiles, employee links, opening rules, board projection,
# internal application overlay, transfers.
from app.models import mobility  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["mobility"]

# Epic 53 (Phase 4 foundation, AD-55..AD-66): agent runtime + payload store, transcripts, the tenant Recording Policy
# and the voice session / interview capture hubs.
from app.models import (  # ruff: ignore[module-import-not-at-top-of-file]
    agent_runtime,
    interview_captures,
    recording_policy,
    transcripts,
    voice,
)

__all__ += ["agent_runtime", "interview_captures", "recording_policy", "transcripts", "voice"]

# Epic 54 (Recruitment Copilot, AD-64): private conversations, messages, feedback and tenant Copilot settings.
from app.models import copilot  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["copilot"]

# Epic 64 (Analytics Agent, FR-316..FR-318): private analytics answers (definition + viz + explanation; no rows).
from app.models import analytics_answers  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["analytics_answers"]

# Epic 57 (Screening Agent, FR-289..FR-292): versioned questionnaires + bindings, settings, conversations, turns, answers.
from app.models import screening_agent  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["screening_agent"]

# Epic 55 (Demand Agent): intake settings / routes / sender domains / messages and the insights tables (field sources,
# suggestions, flags).
from app.models import demand_insights, demand_intake  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["demand_insights", "demand_intake"]

# Epic 56 (Sourcing & Matching Agent): agent shortlists (versions), entries, dismissals and the tenant settings.
from app.models import sourcing_agent  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["sourcing_agent"]

# Epic 59 (Scheduling Agent, FR-297..FR-300): scheduling requests, slot offers, history, interviewer preferences, the
# tenant settings and the reminder log.
from app.models import scheduling_agent  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["scheduling_agent"]

# Epic 61 (Submission Agent, FR-305..FR-307): per-Client submission rules and prepared submission packs.
from app.models import submission_packs  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["submission_packs"]

# Epic 62 (Compliance, BGV & Onboarding Agents, FR-308..FR-310): Knowledge Sources, Onboarding Q&A, chases, follow-ups.
from app.models import chasing, knowledge  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["chasing", "knowledge"]

# Epic 63 (Timesheet & Billing/Collections Agents, FR-311..FR-315): timesheet anomaly flags, collection rules, agent
# holds and the Collections Agent log.
from app.models import collection_rules, timesheet_flags  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["collection_rules", "timesheet_flags"]

# Epic 65 (Phase 4 governance, FR-319..FR-330): platform evaluation results and Proposal review signals (SM-C14).
from app.models import ai_evals, proposal_signals  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["ai_evals", "proposal_signals"]

# Epic 66 (Phase 5 foundation, AD-73..AD-92): Apps, Installations and the OAuth token store, ecosystem controls and
# exposures, the Secrets Vault, the transactional outbox, connector runtime state and usage metering.
from app.models import (  # ruff: ignore[module-import-not-at-top-of-file]
    ecosystem,
    integrations,
    metering,
    oauth,
    public_events,
    vault,
)

__all__ += ["ecosystem", "integrations", "metering", "oauth", "public_events", "vault"]

# Epic 67 (Public API & OAuth Apps): public bulk import jobs and deprecation notices.
from app.models import public_api  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["public_api"]

# Epic 76 (Real Telephony & STT): telephony settings, caller IDs, recruiter call phones, voice calls and STT jobs.
from app.models import telephony  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["telephony"]

# Epic 68 (Webhooks): subscriptions, the delivery idempotency keys, the partitioned delivery log and the sandbox echo.
from app.models import webhooks  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["webhooks"]

# Epic 69 (Integration Marketplace): listings, versions and reviews (platform) and the installation health log.
from app.models import marketplace  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["marketplace"]

# Epic 78 (Phase 4 hardening): AI campaign copy requests and drafts.
from app.models import campaign_copy  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["campaign_copy"]

# Epic 73 (Governed MCP Server): MCP sessions, access holds and the tenant MCP settings.
from app.models import mcp  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["mcp"]

# Epic 70 (Job Board Connectors): board postings, board sets, field mappings, applicant ingests, suppressions, credits
# and the job feed / job board settings.
from app.models import job_distribution  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["job_distribution"]

# Epic 71 (HRMS & Accounting Connectors): HRMS field mappings / sync records / settings and ledger mappings / sync
# records / runs / payment suggestions / settings.
from app.models import hrms_sync, ledger_sync  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["hrms_sync", "ledger_sync"]

# Epic 72 (Calendar Sync): push channels, event links, sync settings, free/busy opt-outs and the channel lookup.
from app.models import calendar_sync  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["calendar_sync"]

# Epic 74 (Developer Portal): developer accounts, partner org members / invitations / verification, the org audit log,
# self-serve sandboxes and conformance runs (platform tables).
from app.models import developer  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["developer"]

# Epic 77 (Copilot in Portals): portal Copilot conversations, messages, feedback and settings.
from app.models import portal_copilot  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["portal_copilot"]

# Epic 75 (Cross-tenant Job Network): platform index tables + tenant opt-in tables.
from app.models import job_network  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["job_network"]

# Epic 79 (Phase 5 Governance): access reviews and the DSR erasure fan-out register.
from app.models import ecosystem_governance  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["ecosystem_governance"]

# Unified platform (hub ingest): inbound data loops — Croar assessments, Feedback IQ engagement signals.
from app.models import ingest  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["ingest"]

# Per-tenant outbound email (SMTP) configuration — one row per tenant (org-configured sending mailbox).
from app.models import mail_settings  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["mail_settings"]

# Unified platform (spoke -> hub): per-tenant integration credentials (platform table; encrypted ATS key).
from app.models import integration_credentials  # ruff: ignore[module-import-not-at-top-of-file]

__all__ += ["integration_credentials"]
