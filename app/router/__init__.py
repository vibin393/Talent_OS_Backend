"""Layered-layout routers (AD-1 amended): ``app/router/<domain>.py``.

``get_layered_routers()`` returns the routers mounted under ``/api/v1`` by ``app.api`` (imported lazily so
importing ``app.router.base`` stays cheap). Order matters: generic paths (``/{entity_type}/{id}/attachments``)
come last.
"""

from fastapi import APIRouter


def get_layered_routers() -> list[APIRouter]:
    from app.router import activity, ai, approvals, candidates, clients, demands, files

    return [
        *_identity_routers(),
        ai.router,
        candidates.router,
        candidates.skills_router,
        candidates.tags_router,
        candidates.lists_router,
        clients.router,
        clients.contacts_router,
        *_epic55_routers(),  # before demands.router: /demands/intake-messages|flags|... precede /demands/{id}
        demands.router,
        demands.settings_router,
        approvals.chains_router,
        approvals.router,
        files.router,
        *_epic40_routers(),  # before Epic 6: /applications/assessment-badges precedes /applications/{id}
        *_epic6_routers(),
        *_epic7_routers(),
        *_epic8_routers(),
        *_epic33_routers(),  # before Epic 9: /public/careers/resolve-host precedes /public/careers/{slug}
        *_epic9_routers(),
        *_epic11_routers(),
        *_epic12_routers(),
        *_epic10_routers(),
        *_epic5_routers(),
        *_epic72_routers(),  # before Epic 13: /hooks/calendar/{channel_id} precedes /hooks/{category}/{config_id}
        *_epic13_routers(),
        *_epic14_routers(),
        *_epic16_routers(),
        *_epic15_routers(),
        *_epic32_routers(),
        *_epic22_routers(),
        *_epic31_routers(),  # before Epic 19: /placements/ending-soon precedes /placements/{id}
        *_epic19_routers(),
        *_epic17_routers(),
        *_epic24_routers(),
        *_epic21_routers(),
        *_epic23_routers(),
        *_epic20_routers(),
        *_epic25_routers(),
        *_epic26_routers(),
        *_epic27_routers(),
        *_epic51_routers(),  # before Epic 29: /billing/dsc/pending precedes /billing/{kind}/{id}
        *_epic29_routers(),
        *_epic30_routers(),
        *_epic28_routers(),
        *_epic18_routers(),
        *_epic35_routers(),
        *_epic36_routers(),
        *_epic53_routers(),
        *_epic54_routers(),
        *_epic64_routers(),
        *_epic56_routers(),
        *_epic57_routers(),
        *_epic59_routers(),
        *_epic60_routers(),
        *_epic58_routers(),
        *_epic61_routers(),
        *_epic62_routers(),
        *_epic63_routers(),
        *_epic65_routers(),
        *_epic45_routers(),
        *_epic37_routers(),
        *_epic50_routers(),
        *_epic46_routers(),
        *_epic47_routers(),
        *_epic42_routers(),
        *_epic38_routers(),
        *_epic48_routers(),
        *_epic49_routers(),
        *_epic41_routers(),
        *_epic39_routers(),
        *_epic44_routers(),
        *_epic43_routers(),
        *_epic66_routers(),
        *_epic67_routers(),
        *_epic68_routers(),
        *_epic69_routers(),
        *_epic78_routers(),
        *_epic76_routers(),
        *_epic73_routers(),
        *_epic74_routers(),
        *_epic70_routers(),
        *_epic77_routers(),
        *_epic71_routers(),
        *_epic75_routers(),
        *_epic79_routers(),
        activity.router,  # generic /{entity}/{id}/timeline|activities: keep last
    ]


def _identity_routers() -> list[APIRouter]:
    """Identity (Epic 1): operator sign-in / tenant provisioning, tenant-user auth, tenant security and
    tenant administration. Mounted first, in this order."""
    from app.router import admin, auth, entitlements, platform

    return [platform.operator_router, auth.auth_router, auth.tenant_router, admin.router, entitlements.router]


def _epic12_routers() -> list[APIRouter]:
    """Privacy, platform operations, notices, API keys, settings hub and public links (Epic 12)."""
    from app.router import api_keys, notices, platform, privacy, public, settings

    return [
        privacy.router,
        privacy.consents_router,
        platform.router,
        notices.router,
        api_keys.router,
        settings.router,
        public.router,
    ]


def _epic6_routers() -> list[APIRouter]:
    """Pipeline, screening, submissions and client feedback (Epic 6)."""
    from app.router import pipeline

    return [
        pipeline.router,
        pipeline.demand_router,
        pipeline.applications_router,
        pipeline.screenings_router,
        pipeline.submissions_router,
        pipeline.feedback_router,
        pipeline.misc_router,
    ]


def _epic7_routers() -> list[APIRouter]:
    """Interviews (plans, kits, templates, scheduling, scorecards, outcomes) and calendar / mailbox
    connections (Epic 7). The public scorecard link lives on ``public.router``."""
    from app.router import connections, interviews

    return [
        interviews.router,
        interviews.kits_router,
        interviews.templates_router,
        interviews.demand_router,
        interviews.applications_router,
        interviews.me_router,
        connections.router,
    ]


def _epic10_routers() -> list[APIRouter]:
    """Email, tasks, notifications and workflow rules (Epic 10). Generic ``/{entity}/{id}/emails|tasks``
    routes come after every specific router (still before the activity router)."""
    from app.router import comms, notifications, tasks, workflow

    return [
        comms.templates_router,
        comms.emails_router,
        comms.webhooks_router,
        tasks.router,
        notifications.router,
        notifications.me_router,
        workflow.router,
        comms.entity_router,
        tasks.entity_router,
    ]


def _epic5_routers() -> list[APIRouter]:
    """Search (candidate + global) and matching (demand matches, match actions, matching settings), Epic 5."""
    from app.router import matching, search

    return [search.router, matching.router]


def _epic8_routers() -> list[APIRouter]:
    """Offers, versions, letters, templates and the Candidate Portal offer API (Epic 8). Public secure-link
    offer pages live on ``public.router`` (Epic 12 / Epic 6)."""
    from app.router import offers, portal_offers

    return [
        offers.router,
        offers.applications_router,
        offers.demands_router,
        offers.templates_router,
        portal_offers.router,
    ]


def _epic9_routers() -> list[APIRouter]:
    """Career Portal (public), candidate sign-in, Candidate Portal and career-site administration (Epic 9)."""
    from app.router import career_site_admin, careers, portal

    return [
        careers.router,
        careers.auth_router,
        careers.links_router,
        portal.router,
        career_site_admin.router,
        career_site_admin.postings_router,
    ]


def _epic11_routers() -> list[APIRouter]:
    """Dashboards, report lists and exports (Epic 11)."""
    from app.router import reporting

    return [reporting.router, reporting.exports_router]


def _epic13_routers() -> list[APIRouter]:
    """Phase 2 foundation (Epic 13): legal entities / FX, connectors, public connector webhooks and the
    external portals' ``/me`` (client, vendor, worker)."""
    from app.router import (
        client_portal,
        connector_hooks,
        connectors,
        legal_entities,
        vendor_portal,
        worker_portal,
    )

    return [
        legal_entities.router,
        connectors.router,
        connector_hooks.router,
        client_portal.router,
        vendor_portal.router,
        worker_portal.router,
    ]


def _epic14_routers() -> list[APIRouter]:
    """Phase 2 access (Epic 14): SSO administration and sign-in, internal Client / Vendor User access
    administration, and the Client / Vendor Portal sign-in endpoints (on client_portal / vendor_portal)."""
    from app.router import client_portal, external_access, sso, vendor_portal

    return [
        sso.router,
        sso.auth_router,
        external_access.router,
        client_portal.auth_router,
        vendor_portal.auth_router,
    ]


def _epic15_routers() -> list[APIRouter]:
    """Sales CRM (Epic 15): leads, imports, conversion, opportunities, Won conversion, dashboard, settings."""
    from app.router import crm

    return [crm.router]


def _epic16_routers() -> list[APIRouter]:
    """Client commercials (Epic 16): payment terms, billing profiles, contracts, rate cards, rate checks, POs."""
    from app.router import commercials

    return [commercials.router]


def _epic32_routers() -> list[APIRouter]:
    """SLA targets / clocks / escalation policies (Epic 32). Workflow graph endpoints extend ``workflow``."""
    from app.router import sla

    return [sla.router]


def _epic22_routers() -> list[APIRouter]:
    """Documents & eSignature (Epic 22): templates, repository, signature requests, internal signing, verify;
    Worker signatures and the offer acceptance code; public secure-link signing and certificate check."""
    from app.router import documents, public_esign, worker_portal_documents

    return [
        documents.router,
        worker_portal_documents.router,
        worker_portal_documents.offers_router,
        public_esign.router,
    ]


def _epic33_routers() -> list[APIRouter]:
    """Communication extensions (Epic 33): SMS / WhatsApp, reply capture, email threads, custom career
    domains (+ public host resolution) and the candidate portal Messages section."""
    from app.router import messaging

    return [
        messaging.public_router,
        messaging.comms_router,
        messaging.reply_router,
        messaging.domain_router,
        messaging.portal_router,
    ]


def _epic19_routers() -> list[APIRouter]:
    """Placements, pre-joining, Worker 360 and the Worker "My Work" placement pages (Epic 19); the public
    joining-confirmation response (``/public/links/{token}/joining``)."""
    from app.router import placements, worker_portal_placements

    return [
        placements.router,
        placements.workers_router,
        placements.public_router,
        worker_portal_placements.router,
    ]


def _epic17_routers() -> list[APIRouter]:
    """Vendors & Vendor Portal (Epic 17): internal vendor management, releases, review queue, statements, and
    the Vendor Portal content routes (``/vendor-portal/*``; Epic 14 owns ``vendor_portal.py``)."""
    from app.router import vendor_portal_vms, vendors

    return [vendors.router, vendor_portal_vms.router]


def _epic24_routers() -> list[APIRouter]:
    """Bench management (Epic 24): ``/bench/*`` and the Worker "My Work" bench page (``/portal/work/bench``)."""
    from app.router import bench, worker_portal_bench

    return [bench.router, worker_portal_bench.router]


def _epic21_routers() -> list[APIRouter]:
    """Compliance & credentials (Epic 21): ``/compliance/*``, ``/credentials/*``, candidate credentials, the
    placement checklist / waiver and the Worker "My Work" credentials page (``/portal/work/credentials``)."""
    from app.router import compliance, worker_portal_compliance

    return [
        compliance.router,
        compliance.credentials_router,
        compliance.candidates_router,
        compliance.placements_router,
        worker_portal_compliance.router,
    ]


def _epic23_routers() -> list[APIRouter]:
    """Digital onboarding (Epic 23): templates, settings, cases, task review and the Worker "My Work"
    onboarding pages (``/portal/work/onboarding``)."""
    from app.router import onboarding, worker_portal_onboarding

    return [
        onboarding.router,
        onboarding.cases_router,
        onboarding.tasks_router,
        worker_portal_onboarding.router,
    ]


def _epic20_routers() -> list[APIRouter]:
    """BGV & reference checks (Epic 20): internal API, Worker "My Work" BGV, Client Portal BGV status and the
    public provider / referee secure-link pages."""
    from app.router import bgv, client_portal_bgv, public_bgv, worker_portal_bgv

    return [bgv.router, worker_portal_bgv.router, client_portal_bgv.router, public_bgv.router]


def _epic25_routers() -> list[APIRouter]:
    """Timesheets (Epic 25): internal ``/time/*``, the Worker "My Work" timesheets (``/portal/work/timesheets``)
    and the Client Portal timesheets (``/client-portal/timesheets``: list, detail, decide, bulk)."""
    from app.router import client_portal_timesheets, time, worker_portal_time

    return [time.router, worker_portal_time.router, client_portal_timesheets.router]


def _epic27_routers() -> list[APIRouter]:
    """Expenses (Epic 27): internal ``/expenses/*``, the Worker "My Work" expenses (``/portal/work/expense-*``)
    and the Client Portal expenses (``/client-portal/expenses``: list, detail, per-line decide, receipts)."""
    from app.router import client_portal_expenses, expenses, worker_portal_expenses

    return [expenses.router, worker_portal_expenses.router, client_portal_expenses.router]


def _epic26_routers() -> list[APIRouter]:
    """Leave & Holidays (Epic 26): internal ``/time/holiday-calendars|leave-*``, the Worker "My Work" leave
    (``/portal/work/leave-*``, ``/holidays``, ``/optional-holidays``) and the Client Portal leave calendar."""
    from app.router import client_portal_leave, leave, worker_portal_leave

    return [leave.router, worker_portal_leave.router, client_portal_leave.router]


def _epic29_routers() -> list[APIRouter]:
    """Billing & GST invoicing (Epic 29): internal ``/billing/*`` and the Client Portal invoices
    (``/client-portal/invoices``)."""
    from app.router import billing, client_portal_billing

    return [billing.router, client_portal_billing.router]


def _epic28_routers() -> list[APIRouter]:
    """Payroll Export (Epic 28): internal ``/payroll/*`` (periods, previews, finalize, files, mappings)."""
    from app.router import payroll

    return [payroll.router]


def _epic31_routers() -> list[APIRouter]:
    """Extension, conversion & offboarding (Epic 31): internal ``/placements/{id}/extensions|conversion|
    offboarding|early-termination``, ``/extensions/*``, ``/offboarding-cases/*``, ``/offboarding-items/*``,
    ``/offboarding/*``; the Client Portal ``/client-portal/placements`` and the two Secure Link pages."""
    from app.router import client_portal_placement_exit, placement_exit

    return [placement_exit.router, placement_exit.public_router, client_portal_placement_exit.router]


def _epic30_routers() -> list[APIRouter]:
    """Receivables, collections & period close (Epic 30): ``/receivables/*``, ``/reconciliation/*``,
    ``/periods/*`` and the Client Portal statement (``/client-portal/receivables``)."""
    from app.router import client_portal_receivables, periods, receivables

    return [receivables.router, receivables.recon_router, periods.router, client_portal_receivables.router]


def _epic18_routers() -> list[APIRouter]:
    """Client Portal content (Epic 18): hiring, approvals hub / delegation, workforce (``/client-portal/*``), the
    internal slot offers / compliance packs and the public ``client_approval`` link page."""
    from app.router import (
        client_portal_admin,
        client_portal_approvals,
        client_portal_hiring,
        client_portal_workforce,
    )

    return [
        client_portal_hiring.router,
        client_portal_approvals.router,
        client_portal_workforce.router,
        client_portal_admin.router,
        client_portal_admin.public_router,
    ]


def _epic35_routers() -> list[APIRouter]:
    """Privacy platform (Epic 35): go-live import (``/golive/*``), the audit record timeline and the portal
    notification inboxes (``/client-portal|vendor-portal|portal/work/notifications``)."""
    from app.router import audit_timeline, golive, portal_notifications

    return [golive.router, audit_timeline.router, *portal_notifications.routers()]


def _epic36_routers() -> list[APIRouter]:
    """Phase 3 foundation (Epic 36): the semantic layer catalogue / validation (``/semantic/*``)."""
    from app.router import semantic

    return [semantic.router]


def _epic37_routers() -> list[APIRouter]:
    """Skills graph & proficiency (Epic 37): ``/skill-graph/*``, candidate skill evidence / suggestions
    (``/candidates/{id}/skills/...``, ``/candidates/{id}/skill-suggestions``) and ``/skills/insights``."""
    from app.router import skills

    return list(skills.routers)


def _epic50_routers() -> list[APIRouter]:
    """Client Portal SSO (Epic 50): per-Client IdP administration / enforcement (``/clients/{id}/sso/*``) and the
    Client Portal discovery / start (``/client-portal/auth/sso/*``); callbacks are Epic 14's."""
    from app.router import client_sso

    return [client_sso.router, client_sso.portal_router]


def _epic46_routers() -> list[APIRouter]:
    """Immigration (Epic 46): ``/immigration/*``, the Worker portal ``/portal/work/immigration/*`` and the public
    counsel / Candidate upload page ``/public/links/{token}/immigration-upload``."""
    from app.router import immigration, worker_portal_immigration

    return [immigration.router, worker_portal_immigration.router, immigration.public_router]


def _epic51_routers() -> list[APIRouter]:
    """Finance carry-overs (Epic 51): DSC policies / signing (``/dsc/*``, ``/billing/dsc/pending``,
    ``/billing/{invoices|credit-notes}/{id}/dsc/*``), 26AS reconciliation (``/receivables/tds-recon/*``) and the
    live FX feed (``/fx-feed/*``)."""
    from app.router import dsc, fx_feed, tds_recon

    return [dsc.router, tds_recon.router, fx_feed.router]


def _epic45_routers() -> list[APIRouter]:
    """SOW & project staffing (Epic 45): ``/projects/*``, ``/project-roles/*``, ``/project-assignments/*``,
    ``/deliverables/*`` and the Client Portal ``/client-portal/projects`` / ``/client-portal/deliverables/*``."""
    from app.router import client_portal_projects, projects

    return [projects.router, client_portal_projects.router]


def _epic47_routers() -> list[APIRouter]:
    """Custom report builder & scheduled reports (Epic 47): ``/reports/custom/*``, ``/me/report-pins``,
    ``/report-schedules/*`` and ``/report-runs/{id}/download``."""
    from app.router import report_builder

    return [report_builder.router]


def _epic42_routers() -> list[APIRouter]:
    """Employee referrals (Epic 42): ``/referral-programs``, ``/referral-settings``, ``/me/referrals/*``
    (self-service), ``/referrals/*``, ``/referral-rewards/*``, the Worker portal ``/portal/work/referrals/*`` and the
    public referee page ``/public/links/{token}/referral``."""
    from app.router import public_referral, referrals, worker_portal_referrals

    return [referrals.router, worker_portal_referrals.router, public_referral.router]


def _epic38_routers() -> list[APIRouter]:
    """Talent pools & rediscovery (Epic 38): ``/segments/*``, ``/talent-pools/*`` and the Demand
    ``/demands/{id}/pool-pins`` / ``/demands/{id}/pools/{pool_id}/add-to-pipeline`` endpoints (the rediscovery
    summary ``/demands/{id}/rediscovery`` is in ``router/matching.py``)."""
    from app.router import segments, talent_pools

    return [segments.router, talent_pools.router]


def _epic40_routers() -> list[APIRouter]:
    """Assessments (Epic 40): ``/assessment-items/*``, ``/assessments/*``, ``/assessment-invitations/*``,
    ``/assessment-reviews``, ``/attempt-answers/*``, ``/attempts/*``, ``/assessment-settings``,
    ``/candidates/{id}/assessment-results``, ``/applications/assessment-badges``,
    ``/submissions/{id}/assessment-shares``, the Candidate Portal ``/portal/assessments/*`` and the Secure Link page
    ``/public/links/{token}/assessment/*``."""
    from app.router import assessments, portal_assessments, public_assessment

    return [assessments.router, portal_assessments.router, public_assessment.router]


def _epic44_routers() -> list[APIRouter]:
    """Workforce planning (Epic 44): ``/workforce/*`` (plans, lines, import, revisions, slots, conversion, plan vs
    actual, settings) and the Client Portal ``/client-portal/forecasts*`` (not the P2 ``client_portal_workforce``)."""
    from app.router import client_portal_forecasts, workforce

    return [workforce.router, client_portal_forecasts.router]


def _epic48_routers() -> list[APIRouter]:
    """Predictive analytics (Epic 48): ``/predictions``, ``/predictions/batch``, ``/predictions/{id}/explanation``,
    ``/analytics/models/*`` and ``/settings/analytics`` (internal Users only; no portal router)."""
    from app.router import analytics

    return [analytics.router]


def _epic41_routers() -> list[APIRouter]:
    """Internal mobility (Epic 41): ``/mobility/*`` (board, apply, my applications, settings, eligibility rules,
    employee grades, recruiter views, transfer confirmation) and the Worker portal ``/portal/work/opportunities*``."""
    from app.router import mobility, worker_portal_mobility

    return [mobility.router, worker_portal_mobility.router]


def _epic39_routers() -> list[APIRouter]:
    """Recruitment marketing (Epic 39): ``/marketing/*`` (settings, audiences, campaigns, sequences, enrolments,
    analytics) and the public tracking / unsubscribe links ``/public/m/{token}/*``."""
    from app.router import campaign_public, campaigns

    return [campaigns.router, campaign_public.router]


def _epic49_routers() -> list[APIRouter]:
    """Bias audit (Epic 49): internal ``/fairness/*``, Candidate Portal ``/portal/self-id`` +
    ``/portal/alternative-requests`` and public ``/public/fairness/{slug}/*``. These three routers (with the
    fairness task and erasure handler) are the only importers of the fairness models (``fairness-isolation``)."""
    from app.router import fairness, fairness_public, portal_self_id

    return [fairness.router, portal_self_id.router, fairness_public.router]


def _epic43_routers() -> list[APIRouter]:
    """Campus hiring & recruitment events (Epic 43): ``/campus/*`` (internal) and the public registration pages
    ``/public/campus/r/{token}`` / ``/public/campus/e/{token}``."""
    from app.router import campus, public_campus

    return [campus.router, public_campus.router]


def _epic53_routers() -> list[APIRouter]:
    """Phase 4 foundation (Epic 53): ``/ai/agentic``, ``/ai/kill-switch``, ``/agents/*``, ``/agent-runs/*`` and
    ``/proposals/*`` (internal principals; deciding is human only). ``/agents/tools`` precedes ``/agents/{key}``."""
    from app.router import agent_proposals, agents

    return [agents.router, agent_proposals.router]


def _epic54_routers() -> list[APIRouter]:
    """Recruitment Copilot (Epic 54): ``/copilot/*`` -- status, private conversations and messages, the SSE turn
    stream, cancel, feedback, settings and admin usage (metadata only)."""
    from app.router import copilot

    return [copilot.router]


def _epic64_routers() -> list[APIRouter]:
    """Analytics Agent (Epic 64): ``/analytics/answers/*`` (private answers: read, re-preview as the viewer, link a saved
    report). Asking goes through the Copilot endpoints (Epic 54)."""
    from app.router import analytics_answers

    return [analytics_answers.router]


def _epic56_routers() -> list[APIRouter]:
    """Sourcing & Matching Agent (Epic 56): ``/demands/{id}/agent-shortlist*``, ``/demands/{id}/sourcing-runs``,
    ``/agent-shortlist-entries/{id}/decision`` and ``/settings/agents/sourcing``."""
    from app.router import sourcing_agent

    return [sourcing_agent.router]


def _epic55_routers() -> list[APIRouter]:
    """Demand Agent (Epic 55): ``/inbound/demand-intake`` (public, HMAC), ``/demands/intake-messages*``,
    ``/demands/intake-settings*``, ``/demands/intake-sender-domains*``, ``/demands/agent-settings``, ``/demands/flags*``,
    ``/demands/{id}/insights`` and ``/demands/{id}/suggestions/*``."""
    from app.router import demand_insights, demand_intake

    return [demand_intake.inbound_router, demand_intake.router, demand_insights.router]


def _epic57_routers() -> list[APIRouter]:
    """Screening Agent (Epic 57): internal ``/screening-questionnaires/*``, ``/screening-conversations/*``,
    ``/screening-reviews*``, ``/settings/agents/screening``; Candidate Portal ``/portal/screening-chats/*``; public
    ``/public/screening-chat/{token}/*``."""
    from app.router import portal_screening_chat, public_screening_chat, screening_agent

    return [screening_agent.router, portal_screening_chat.router, public_screening_chat.router]


def _epic59_routers() -> list[APIRouter]:
    """Scheduling Agent (Epic 59): internal ``/applications/{id}/scheduling-*``, ``/scheduling-requests/*``,
    ``/interviews/{id}/reschedule-requests``, ``/me|users/{id}/scheduling-preferences``, ``/settings/agents/scheduling``;
    Candidate Portal ``/portal/scheduling*`` and ``/portal/interviews/{id}/reschedule-request``; public
    ``/public/slot-pick/{token}/*``."""
    from app.router import portal_scheduling, public_scheduling, scheduling_agent

    return [scheduling_agent.router, portal_scheduling.router, public_scheduling.router]


def _epic58_routers() -> list[APIRouter]:
    """Voice Screening (Epic 58): internal ``/applications/{id}/voice-sessions``, ``/voice-sessions/*``,
    ``/settings/agents/voice-screening``, ``/recording-policies*``; public ``/public/voice-consent/{token}``."""
    from app.router import public_voice_consent, recording_policy, voice

    return [voice.router, recording_policy.router, public_voice_consent.router]


def _epic60_routers() -> list[APIRouter]:
    """Interview Assistant (Epic 60): internal ``/interviews/{id}/capture*``, ``/interviews/{id}/scorecard-draft*``,
    ``/me/interview-recording-ack``, ``/settings/agents/interview-assistant``; Candidate Portal
    ``/portal/interviews/{id}/recording-consent``; public ``/public/interview-recording-consent/{token}`` and
    ``/public/interview-capture-ack/{token}``."""
    from app.router import interview_captures, portal_interview_consent, public_interview_consent

    return [interview_captures.router, portal_interview_consent.router, public_interview_consent.router]


def _epic61_routers() -> list[APIRouter]:
    """Submission Agent (Epic 61): ``/applications/{id}/submission-pack*``, ``/submission-packs*`` and
    ``/clients/{id}/submission-rules``."""
    from app.router import submission_packs

    return [submission_packs.router]


def _epic62_routers() -> list[APIRouter]:
    """Compliance, BGV & Onboarding Agents (Epic 62): ``/knowledge/*``, ``/onboarding-qa/*``,
    ``/compliance/pre-start-risk``, ``/candidates|placements/{id}/chases``, ``/bgv/cases/{id}/follow-ups``; Worker portal
    ``/portal/work/help/*``."""
    from app.router import chasing, knowledge, worker_portal_onboarding_qa

    return [knowledge.router, chasing.router, worker_portal_onboarding_qa.router]


def _epic63_routers() -> list[APIRouter]:
    """Timesheet & Billing/Collections Agents (Epic 63): ``/timesheets/{id}/flags``, ``/timesheet-flags*``,
    ``/collections/rules*``, ``/collections/holds*``, ``/collections/clients/{id}/plan|agent-state|agent-log``,
    ``/invoices/{id}/agent-log`` and ``/billing/prepare-run``."""
    from app.router import collection_rules, timesheet_flags

    return [timesheet_flags.router, collection_rules.router]


def _epic65_routers() -> list[APIRouter]:
    """Phase 4 governance (Epic 65): ``/ai-governance/*`` (console, costs, violations, evals, the Proposal "opened"
    signal) and ``/agents/{key}/checklist|limits|burn-in/end|checklist/acknowledge``."""
    from app.router import ai_governance

    return [ai_governance.router]


def _epic66_routers() -> list[APIRouter]:
    """Phase 5 foundation (Epic 66): ``/ecosystem`` (state, Ecosystem Kill Switch, app policy, MCP enablement, usage).
    The Public API is mounted separately at ``/api/public/v1`` (``app.router.public_v1``)."""
    from app.router import ecosystem

    return [ecosystem.router]


def _epic67_routers() -> list[APIRouter]:
    """Epic 67 (Public API & OAuth Apps): private Apps ``/apps``, ``/me/connected-apps`` and
    ``/api-keys/public-scopes``. The OAuth protocol endpoints (``/api/oauth/*``) are mounted by ``app.main``."""
    from app.router import apps

    return [apps.router]


def _epic68_routers() -> list[APIRouter]:
    """Webhooks (Epic 68): ``/webhooks/*`` (subscriptions, deliveries, replay, test events, echo endpoints) and the
    sandbox Echo receiver ``/webhook-echo/{token}``. The public ``/webhooks`` / ``/event_types`` / ``/events`` routes
    are mounted with the Public API (``app.router.public_v1``)."""
    from app.router import webhook_echo, webhooks

    return [webhooks.router, webhook_echo.router]


def _epic78_routers() -> list[APIRouter]:
    """AI campaign copy (Epic 78): ``/campaign-copy/*`` (requests, drafts, ★ approval, apply, campaign banner status)."""
    from app.router import campaign_copy

    return [campaign_copy.router]


def _epic69_routers() -> list[APIRouter]:
    """Epic 69 (Integration Marketplace): ``/marketplace/*`` (catalogue, installations, policy, processors, SDK) and the
    platform reviewer console ``/platform/marketplace/*``."""
    from app.router import marketplace, platform_marketplace

    return [marketplace.router, platform_marketplace.router]


def _epic76_routers() -> list[APIRouter]:
    """Epic 76 (Real Telephony & STT): ``/telephony/*`` (call options, ★ calls, usage), ``/me/call-phone``,
    ``/settings/telephony*`` and the provider endpoints ``/hooks/voice/{config_id}/answer|exotel/...``."""
    from app.router import voice_calls

    return [voice_calls.router, voice_calls.hooks_router]


def _epic72_routers() -> list[APIRouter]:
    """Epic 72 (Calendar Sync): the public push endpoint ``/hooks/calendar/{channel_id}`` and ``/calendar-sync/*``."""
    from app.router import calendar_sync, hooks_calendar

    return [hooks_calendar.router, calendar_sync.router]


def _epic73_routers() -> list[APIRouter]:
    """Epic 73 (Governed MCP Server): ``/mcp/*`` (settings, enable / disable, tools, sessions, holds, me). The protocol
    endpoint ``/api/mcp`` and the RFC 9728 metadata are mounted by ``app.main``."""
    from app.router import mcp_admin

    return [mcp_admin.router]


def _epic70_routers() -> list[APIRouter]:
    """Epic 70 (Job Board Connectors): ``/demands/{id}/board-postings*``, ``/board-postings/*``, ``/job-boards/*`` and
    the anonymous XML feed ``/public/job-feed/{slug}/{token}.xml``."""
    from app.router import job_distribution, job_feed_public

    return [job_distribution.router, job_feed_public.router]


def _epic77_routers() -> list[APIRouter]:
    """Epic 77 (Copilot in Portals): ``/client-portal/copilot/*``, ``/vendor-portal/copilot/*``, ``/portal/copilot/*``
    (portal principals only; SSE turn streams) and the internal admin ``/copilot/portal-admin/*``."""
    from app.router import (
        candidate_portal_copilot,
        client_portal_copilot,
        copilot_portal_admin,
        vendor_portal_copilot,
    )

    return [client_portal_copilot.router, vendor_portal_copilot.router, candidate_portal_copilot.router, copilot_portal_admin.router]


def _epic74_routers() -> list[APIRouter]:
    """Epic 74 (Developer Portal): ``/developer/*`` (developer sessions: accounts, MFA, orgs, verification, Apps,
    submissions, sandboxes, playground, conformance, usage; the sandbox handoff), ``/developer-tenant/*`` (Tenant
    Developers on the normal session) and the platform console ``/platform/developers/*``."""
    from app.router import developer_portal, developer_tenant, platform_developers

    return [developer_portal.router, developer_tenant.router, platform_developers.router]


def _epic71_routers() -> list[APIRouter]:
    """Epic 71 (HRMS & Accounting Connectors): ``/hrms/*`` (settings, field mappings, sensitive toggles, sync queue,
    Placement panel, import file, pull) and ``/ledger/*`` (settings, mappings, records, runs incl. Tally files, ★
    payment suggestions, reconciliation)."""
    from app.router import hrms_sync, ledger_sync

    return [hrms_sync.router, ledger_sync.router]


def _epic75_routers() -> list[APIRouter]:
    """Epic 75 (Cross-tenant Job Network): the anonymous public site API ``/public/network/*``, the tenant admin
    ``/job-network/*`` and ``/demands/{id}/network``, and the platform Network Moderator console ``/platform/network/*``."""
    from app.router import network_admin, network_public, platform_network

    return [network_public.router, network_admin.router, platform_network.router]


def _epic79_routers() -> list[APIRouter]:
    """Epic 79 (Phase 5 Governance): the ecosystem console ``/ecosystem-console/*`` (Installations, authorisations,
    webhooks, MCP sessions, data flows, violations, access reviews, test results) and the Candidate / Worker
    self-service ``/portal/privacy/data-recipients``."""
    from app.router import ecosystem_console, portal_data_recipients

    return [ecosystem_console.router, portal_data_recipients.router]
