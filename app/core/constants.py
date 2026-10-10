# ** ------------------------------ Celery Setup --------------------------
# Epic 13 (AD-37): every task declares its queue (``QUEUE = ...`` in its ``app/tasks`` module, or ``queue=``).
# Epic 36 (AD-54): Phase 3 adds campaigns, analytics and reports.
QUEUES: tuple[str, ...] = (
    "interactive",
    "finance_runs",
    "connectors",
    "ai",
    "maintenance",
    "campaigns",
    "analytics",
    "reports",
    # Epic 53 (AD-68): background agent steps; Copilot and candidate / worker conversation turns.
    "agents",
    "conversations",
    # Epic 66 (AD-92): outbox dispatch + webhook deliveries; connector sync / push jobs; telephony callbacks + STT.
    "webhooks",
    "ecosystem",
    "telephony",
)
CEL_DEFAULT_QUEUE = "interactive"
CEL_MAIN_NAME = "talent_os"
CEL_TASK_PATHS = [
    # Epic 66 (AD-92): outbox relay (webhooks), connector sync runner (ecosystem), metering + ecosystem maintenance.
    "app.tasks.public_events",
    "app.tasks.integrations",
    "app.tasks.metering",
    "app.tasks.public_api",  # Epic 67: public bulk import + monthly deprecation report
    "app.tasks.marketplace",  # Epic 69: catalogue sync, health sweep, automated checks, install counts
    "app.tasks.campaign_copy",  # Epic 78: campaign copy run outcomes (agents) + draft purge (maintenance)
    "app.tasks.knowledge_sealing",  # Epic 78: onboarding Q&A sealing backfill (maintenance)
    "app.tasks.ecosystem",
    "app.tasks.webhooks",  # Epic 68: pump / deliver / verify / health scan / replay range / purge
    "app.tasks.developer_portal",  # Epic 74: sandbox create / reset / delete, conformance finalisation; maintenance jobs
    "app.tasks.mcp",  # Epic 73: end MCP sessions on kill switches / revocation, idle mirror + holds, purge (maintenance)
    "app.tasks.telephony",  # Epic 76: recording fetch / provider delete / STT (telephony); retries + reconcile (maintenance)
    "app.tasks.job_distribution",  # Epic 70: applicant notice, withdrawal suppression, expiry scan + digest (ecosystem)
    "app.tasks.calendar_sync",  # Epic 72: channel ensure / delta sync / renew + poll beats (ecosystem)
    "app.tasks.portal_copilot",  # Epic 77: portal Copilot answers / draft events (conversations), retention purge (maintenance)
    "app.tasks.job_network",  # Epic 75: index sync (ecosystem); reconcile / flush stats / purge (maintenance)
    "app.tasks.ecosystem_governance",  # Epic 79: DSR erasure fan-out (ecosystem); access reviews + sandbox purge (maintenance)
    # Epic 53: agent runtime (run steps, reaper, proposal expiry, budget alerts).
    "app.tasks.agents",
    # Epic 57: Screening Agent (inbound replies, workflow starts, reminders / expiry sweep).
    "app.tasks.screening_agent",
    # Epic 59: Scheduling Agent (picks / counter-proposals, reminder rows, stage exit, reminder dispatch).
    "app.tasks.scheduling_agent",
    # Epic 54: Copilot answers, draft events, titles, retention purge.
    "app.tasks.copilot",
    # Epic 55: Demand Agent intake (mailbox poll on ``connectors``, purge / flag expiry on ``maintenance``).
    "app.tasks.demand_intake",
    "app.tasks.demand_insights",
    # Epic 64: analytics answers retention purge (maintenance).
    "app.tasks.analytics_answers",
    # Epic 56: Sourcing Agent triggers, run follow-ups and the weekly refresh (maintenance).
    "app.tasks.sourcing_agent",
    # Epic 60: Interview Assistant withdrawal / decisions / run follow-ups and the hourly withdrawal sweep (maintenance).
    "app.tasks.interview_captures",
    # Epic 58: Voice Screening (Fake call lifecycle on connectors, withdrawal purge + hourly sweep on maintenance).
    "app.tasks.voice",
    # Epic 62: knowledge reindex / reconcile / failed onboarding runs; compliance + BGV chase ticks (maintenance).
    "app.tasks.knowledge",
    "app.tasks.chasing",
    # Epic 61: Submission pack follow-ups (link / discard / consent re-check / checks-only fallback; maintenance).
    "app.tasks.submission_packs",
    # Epic 63: timesheet flag evaluation (interactive), nudge / collections / billing-prep ticks (maintenance).
    "app.tasks.timesheet_flags",
    "app.tasks.collection_rules",
    # Epic 65: governance (review signals, alternative-process guard) and the weekly live eval run (maintenance).
    "app.tasks.ai_governance",
    # Epic 32: SLA clocks, escalations (workflow waits live in app.tasks.workflow).
    "app.tasks.sla",
    "app.job.tasks",
    "app.tasks.candidates",
    "app.tasks.demands",
    # Epic 12: privacy, platform operations, maintenance.
    "app.tasks.privacy",
    "app.tasks.platform",
    "app.tasks.maintenance",
    # Epic 10: email sending, task reminders, notifications, workflow rules.
    "app.tasks.comms",
    "app.tasks.tasks",
    "app.tasks.notifications",
    "app.tasks.workflow",
    # Epic 5: search projection, embeddings, matching.
    "app.tasks.search",
    "app.tasks.ai",
    "app.tasks.matching",
    # Epic 6: pipeline emails, AI screening summary.
    "app.tasks.pipeline",
    "app.tasks.pipeline_ai",
    # Epic 7: interview invites, reminders, completion, scorecard reminders.
    "app.tasks.interviews",
    # Epic 8: offer letters, emails, expiry.
    "app.tasks.offers",
    # Epic 9: career / candidate portal (confirmation + sign-in emails, exports, purge).
    "app.tasks.portal",
    # Epic 11: async exports, export / active-day cleanup.
    "app.tasks.reporting",
    # Epic 13: Phase 2 foundation (accounting periods beat, provider connector jobs).
    "app.tasks.legal_entities",
    "app.tasks.connectors",
    # Epic 33: reply capture, SMS / WhatsApp jobs, custom career domains.
    "app.tasks.messaging",
    # Epic 16: commercials (contract expiry / renewal reminders, PO alerts, e-sign completion).
    "app.tasks.commercials",
    # Epic 22: eSignature notifications, completion, reminders, expiry, OTP purge.
    "app.tasks.documents",
    # Epic 14: external access (revoke Vendor User sessions when the vendor stops being active).
    "app.tasks.external_auth",
    # Epic 19: placements (offer -> placement, upstream gate events, touchpoints / gate alerts / dropout).
    "app.tasks.placements",
    # Epic 15: sales CRM (lead imports, first-contact status).
    "app.tasks.crm",
    # Epic 17: vendors (document expiry, submission work, pipeline / interview / placement reactions).
    "app.tasks.vendors",
    # Epic 20: BGV (case creation / cancel / consent-withdrawal handlers, daily overdue / lapse / purge beat).
    "app.tasks.bgv",
    # Epic 21: compliance (evaluation on placement / selection events, re-evaluation runs, expiry recompute).
    "app.tasks.compliance",
    # Epic 23: onboarding event handlers and reminders.
    "app.tasks.onboarding",
    # Epic 25: timesheets (placement events, retro rate adjustments, daily periods / reminders / missing).
    "app.tasks.time",
    # Epic 29: billing (invoice run builds, placement hooks, pending-IRN alerts, integrity).
    "app.tasks.billing",
    # Epic 28: payroll export (pay period generation).
    "app.tasks.payroll",
    # Epic 31: extension / offboarding (end-date alerts, end-of-term cases, verifier refresh).
    "app.tasks.placements_exit",
    # Epic 30: receivables (CN application, balance cache, daily PTP / overdue, nightly reconciliation).
    "app.tasks.receivables",
    # Epic 18: client portal (approval secure links, pending-approvals digest).
    "app.tasks.client_portal",
    # Epic 37: skills graph (graph cache / stale matches, evidence handlers, skill inference).
    "app.tasks.skills",
    # Epic 46: immigration (permit back-link, overdue scan).
    "app.tasks.immigration",
    # Epic 51: DSC signing, 26AS reconciliation runs, FX feed beat.
    "app.tasks.finance_p3",
    # Epic 45: projects (burn rebuild beat / fact-event rebuilds, placement-closed assignment ends).
    "app.tasks.projects",
    # Epic 47: report builder (schedule evaluator, per-recipient runs, large exports, repair check, file purge).
    "app.tasks.reports",
    # Epic 40: assessments (reminders, expiry, auto-submit beats, provider invite jobs).
    "app.tasks.assessments",
    # Epic 38: talent pools (refresh runner, skills-graph due marking, 15-minute tenant tick: nightly / digest).
    "app.tasks.talent_pools",
    # Epic 42: referrals (pipeline / placement / payroll handlers, referee link expiry, reward evaluator).
    "app.tasks.referrals",
    # Epic 48: predictive analytics (nightly per-tenant training / scoring, debounced re-score, snapshot purge).
    "app.tasks.analytics",
    "app.tasks.fairness",  # Epic 49: bias audit (queue analytics)
    # Epic 41: internal mobility (board projection sync on Demand events, nightly reconcile).
    "app.tasks.mobility",
    # Epic 39: recruitment marketing (campaign start / send batches, engagement handlers, minute tick, reputation, dormant).
    "app.tasks.campaigns",
    # Epic 44: workforce planning (slot status on DemandOpened / DemandStatusChanged / PlacementActivated).
    "app.tasks.workforce",
    # Epic 43: campus & events (eligibility evaluation, assessment / offer / placement / interview handlers, hourly tick).
    "app.tasks.campus",
]
# ** ------------------------------ Celery Setup:END --------------------------

# DB pool / echo settings live in app.core.settings.Settings (DB_POOL_SIZE, DB_ECHO, ...).
