from celery import Celery
from celery.schedules import crontab

# Periodic tasks (Celery beat). Each entry's task runs per tenant through a fan-out task.
BEAT_SCHEDULE: dict[str, dict[str, object]] = {
    # Story 4.6: recompute demand SLA clocks every 5 minutes (enqueues one task per active tenant).
    "demands-evaluate-slas": {"task": "demands.tasks.evaluate_slas_all", "schedule": 300.0}
}

# Epic 12: privacy, platform operations and maintenance jobs (tenant jobs skip suspended tenants).
BEAT_SCHEDULE.update(
    {
        "privacy-consent-withdrawal-sweep": {"task": "privacy.consent_withdrawal_sweep", "schedule": 900.0},
        "privacy-dsr-due-scan": {"task": "privacy.dsr_due_scan", "schedule": crontab(minute=30, hour=3)},
        # Hourly: previews on the 1st at 02:00 tenant time, auto-executes due runs.
        "privacy-retention-tick": {"task": "privacy.retention_tick", "schedule": crontab(minute=0)},
        # Epic 35: retention-class expiry scan, daily 03:00 IST (= 21:30 UTC), queue ``maintenance``.
        "privacy-retention-expiry-scan": {
            "task": "privacy.retention_expiry_scan",
            "schedule": crontab(minute=30, hour=21),
        },
        "platform-expire-support-grants": {"task": "platform.expire_support_grants", "schedule": 300.0},
        "platform-process-tenant-deletions": {
            "task": "platform.process_tenant_deletions",
            "schedule": crontab(minute=15, hour=4),
        },
        "platform-resume-budget-paused": {
            "task": "platform.resume_budget_paused",
            "schedule": crontab(minute=5, hour=0, day_of_month=1),
        },
        "maintenance-purge-sessions": {"task": "maintenance.purge_sessions", "schedule": crontab(minute=10)},
        "maintenance-purge-password-resets": {
            "task": "maintenance.purge_password_resets",
            "schedule": crontab(minute=20),
        },
        "maintenance-purge-invitations": {
            "task": "maintenance.purge_invitations",
            "schedule": crontab(minute=40, hour=2),
        },
        "maintenance-purge-secure-links": {
            "task": "maintenance.purge_secure_links",
            "schedule": crontab(minute=50, hour=2),
        },
        "maintenance-purge-api-key-request-log": {
            "task": "maintenance.purge_api_key_request_log",
            "schedule": crontab(minute=0, hour=3),
        },
    }
)

# Epic 10: task reminders, notification digest and retention, workflow execution retention.
BEAT_SCHEDULE.update(
    {
        "tasks-send-reminders": {"task": "tasks.send_reminders_all", "schedule": 60.0},
        "notifications-digest-tick": {"task": "notifications.digest_tick_all", "schedule": crontab(minute=5)},
        "notifications-purge-old": {
            "task": "notifications.purge_old_all",
            "schedule": crontab(minute=35, hour=3),
        },
        "workflow-purge-executions": {
            "task": "workflow.purge_executions_all",
            "schedule": crontab(minute=45, hour=3),
        },
    }
)


# Epic 5: safety net draining the debounced embedding / matching dirty sets.
BEAT_SCHEDULE.update({"matching-sweep-dirty": {"task": "matching.tasks.sweep_dirty", "schedule": 120.0}})


# Epic 8: offer expiry (every 15 minutes) and 24 h expiry warnings (hourly); both fan out per active tenant.
BEAT_SCHEDULE.update(
    {
        "offers-expire-due": {"task": "offers.expire_due", "schedule": 900.0},
        "offers-expiry-warnings": {"task": "offers.expiry_warnings", "schedule": crontab(minute=25)},
    }
)


# Epic 9: purge expired apply sessions (+ files), expire data-export files, alert on stale exports.
BEAT_SCHEDULE.update(
    {
        "portal-purge-apply-sessions": {"task": "portal.purge_apply_sessions", "schedule": crontab(minute=7)},
        "portal-expire-exports": {"task": "portal.expire_exports", "schedule": crontab(minute=17)},
        "portal-alert-stale-exports": {"task": "portal.alert_stale_exports", "schedule": crontab(minute=27)},
    }
)


# Epic 11: expired export files (hourly) and SM-3 active-day rows older than 400 days (daily).
BEAT_SCHEDULE.update(
    {
        "reporting-purge-expired-exports": {
            "task": "reporting.purge_expired_exports_all",
            "schedule": crontab(minute=37),
        },
        "reporting-purge-active-days": {
            "task": "reporting.purge_active_days_all",
            "schedule": crontab(minute=55, hour=3),
        },
    }
)


# Epic 7: interview reminders (every 5 min), auto-completion (15 min), missing-scorecard reminders (hourly).
BEAT_SCHEDULE.update(
    {
        "interviews-send-due-reminders": {"task": "interviews.send_due_reminders_all", "schedule": 300.0},
        "interviews-complete-elapsed": {"task": "interviews.complete_elapsed_all", "schedule": 900.0},
        "interviews-scorecard-reminders": {
            "task": "interviews.scorecard_reminders_all",
            "schedule": crontab(minute=25),
        },
    }
)


# Epic 13: accounting periods (daily 00:30 IST = 19:00 UTC) and idempotency-key expiry (daily).
BEAT_SCHEDULE.update(
    {
        "legal-entities-ensure-periods": {
            "task": "legal_entities.ensure_periods_all",
            "schedule": crontab(minute=0, hour=19),
        },
        "maintenance-purge-idempotency-keys": {
            "task": "maintenance.purge_idempotency_keys",
            "schedule": crontab(minute=10, hour=3),
        },
    }
)

# Epic 22: eSignature reminders (hourly), expiry (every 15 minutes), consumed / expired OTP purge (daily).
BEAT_SCHEDULE.update(
    {
        "documents-esign-reminders": {"task": "documents.esign_reminders", "schedule": crontab(minute=12)},
        "documents-esign-expiry": {"task": "documents.esign_expiry", "schedule": 900.0},
        "documents-purge-otps": {"task": "documents.purge_otps", "schedule": crontab(minute=42, hour=3)},
    }
)

# Epic 16: commercials daily jobs (01:30 IST = 20:00 UTC): contract expiry / renewal reminders, PO alerts.
BEAT_SCHEDULE.update(
    {"commercials-daily": {"task": "commercials.daily_all", "schedule": crontab(minute=0, hour=20)}}
)

# Epic 32: generic SLA clocks (SLA_EVAL_INTERVAL_SECONDS, default 60 s), escalations and workflow waits (every
# minute); each fans out per active tenant on queue ``maintenance``.
BEAT_SCHEDULE.update(
    {
        "sla-evaluate-clocks": {"task": "sla.evaluate_clocks_all", "schedule": 60.0},
        "sla-evaluate-escalations": {"task": "sla.evaluate_escalations_all", "schedule": 60.0},
        "workflow-resume-waits": {"task": "workflow.resume_waits_all", "schedule": 60.0},
    }
)

# Epic 33: reply capture (2 min, FR-171 <= 5 min latency), quiet-hours release (5 min), custom domain DNS checks.
BEAT_SCHEDULE.update(
    {
        "comms-sync-replies": {"task": "comms.sync_replies", "schedule": 120.0},
        "messaging-release-deferred": {"task": "messaging.release_deferred", "schedule": 300.0},
        "platform-check-custom-domains": {"task": "platform.check_custom_domains", "schedule": 600.0},
    }
)

# Epic 19: placements daily (02:15 IST = 20:45 UTC): touchpoints, unmet-gate alerts, dropout rules.
BEAT_SCHEDULE.update(
    {"placements-daily": {"task": "placements.daily_all", "schedule": crontab(minute=45, hour=20)}}
)

# Epic 13 (AD-37): exactly one beat sends (Redis lock), see ``app.job.scheduler``.
BEAT_SCHEDULER = "app.job.scheduler:LockedScheduler"


# Epic 17: vendor document expiry / suspension check (02:10 IST = 20:40 UTC), queue ``maintenance``.
BEAT_SCHEDULE.update(
    {
        "vendors-check-documents": {
            "task": "vendors.check_documents_all",
            "schedule": crontab(minute=40, hour=20),
        }
    }
)


# Epic 23: onboarding worker reminders (09:30 IST = 04:00 UTC), queue ``maintenance``.
BEAT_SCHEDULE.update(
    {"onboarding-reminders": {"task": "onboarding.reminders_all", "schedule": crontab(minute=0, hour=4)}}
)

# Epic 21: compliance expiry recompute + alerts. Hourly trigger; each tenant runs at its local
# ``recompute_hour_local`` (02:00 IST by default), once per local date. Queue ``maintenance``.
BEAT_SCHEDULE.update(
    {"compliance-recompute": {"task": "compliance.recompute_all", "schedule": crontab(minute=5)}}
)


# Epic 20: BGV daily (02:20 IST = 20:50 UTC): overdue checks, discrepancy lapse, reminders, evidence purge.
BEAT_SCHEDULE.update({"bgv-daily": {"task": "bgv.daily_all", "schedule": crontab(minute=50, hour=20)}})

# Epic 25: timesheets daily (00:30 IST = 19:00 UTC): period timesheets, reminders, missing flags, digest.
BEAT_SCHEDULE.update({"time-daily": {"task": "time.daily_all", "schedule": crontab(minute=0, hour=19)}})


# Epic 34: reporting summaries -- rebuild the last 3 months at 01:30 IST, verify (FR-177) at 02:30 IST (UTC beat).
BEAT_SCHEDULE.update(
    {
        "reporting-nightly-rebuild": {
            "task": "reporting.nightly_rebuild_all",
            "schedule": crontab(minute=0, hour=20),
        },
        "reporting-verify-summaries": {
            "task": "reporting.verify_summaries_all",
            "schedule": crontab(minute=0, hour=21),
        },
    }
)


# Epic 46: immigration overdue scan (07:00 IST = 01:30 UTC), queue ``maintenance``.
BEAT_SCHEDULE.update(
    {
        "immigration-overdue-scan": {
            "task": "immigration.overdue_scan_all",
            "schedule": crontab(minute=30, hour=1),
        }
    }
)


# Epic 40: assessments -- reminders hourly, expiry every 5 minutes, auto-submit of overdue attempts every minute.
BEAT_SCHEDULE.update(
    {
        "assessments-reminders": {"task": "assessments.reminders_all", "schedule": crontab(minute=10)},
        "assessments-expire": {"task": "assessments.expire_all", "schedule": crontab(minute="*/5")},
        "assessments-auto-submit": {"task": "assessments.auto_submit_all", "schedule": crontab(minute="*")},
    }
)


def register_celery_schedules(celery_app: Celery) -> None:
    celery_app.conf.beat_schedule = dict(BEAT_SCHEDULE)
    celery_app.conf.beat_scheduler = BEAT_SCHEDULER


# Epic 26: leave daily (00:45 IST = 19:15 UTC): monthly accrual on accrual_day, year-end carry-forward, verify.
BEAT_SCHEDULE.update(
    {"leave-daily": {"task": "time.leave_daily_all", "schedule": crontab(minute=15, hour=19)}}
)

# Epic 29: pending-IRN alerts hourly; billing integrity nightly (02:30 IST = 21:00 UTC).
BEAT_SCHEDULE.update(
    {
        "billing-pending-irn": {"task": "billing.pending_irn_alerts_all", "schedule": crontab(minute=5)},
        "billing-integrity": {"task": "billing.integrity_all", "schedule": crontab(minute=0, hour=21)},
    }
)

# Epic 28: pay periods 12 months ahead, daily (01:00 IST = 19:30 UTC).
BEAT_SCHEDULE.update(
    {
        "payroll-pay-periods": {
            "task": "payroll.ensure_pay_periods_all",
            "schedule": crontab(minute=30, hour=19),
        }
    }
)

# Epic 31: end-date alerts, end-of-term cases and the nightly offboarding refresh (02:00 IST = 20:30 UTC).
BEAT_SCHEDULE.update(
    {"placements-exit-daily": {"task": "placements_exit.daily_all", "schedule": crontab(minute=30, hour=20)}}
)

# Epic 30: receivables daily (PTP check + overdue scan, 01:00 IST = 19:30 UTC); cache rebuild + reconciliation
# nightly (03:00 IST = 21:30 UTC).
BEAT_SCHEDULE.update(
    {
        "receivables-daily": {"task": "receivables.daily_all", "schedule": crontab(minute=30, hour=19)},
        "receivables-nightly": {"task": "receivables.nightly_all", "schedule": crontab(minute=30, hour=21)},
    }
)

# Story 22.9 (A10): create-from-resume drafts older than 7 days, daily 03:20 IST (= 21:50 UTC).
BEAT_SCHEDULE.update(
    {
        "candidates-purge-draft-resumes": {
            "task": "candidates.purge_draft_resumes",
            "schedule": crontab(minute=50, hour=21),
        }
    }
)

# Epic 51: FX feed hourly (each tenant fetches once per business day after its fetch hour, IST); pending DSC
# signatures digest daily at 09:30 IST (= 04:00 UTC).
BEAT_SCHEDULE.update(
    {
        "fx-feed-fetch": {"task": "finance_p3.fx_feed_fetch_all", "schedule": crontab(minute=15)},
        "dsc-pending-digest": {
            "task": "finance_p3.dsc_pending_digest_all",
            "schedule": crontab(minute=0, hour=4),
        },
    }
)

# Epic 45: project burn read model rebuild, daily 02:30 IST (= 21:00 UTC), queue ``maintenance``.
BEAT_SCHEDULE.update(
    {"projects-rebuild-burn": {"task": "projects.rebuild_burn_all", "schedule": crontab(minute=0, hour=21)}}
)

# Epic 47: scheduled-report evaluator every 5 minutes (queue ``reports``); nightly registry repair check of every
# report definition (02:45 IST = 21:15 UTC) and hourly purge of expired report files (queue ``maintenance``).
BEAT_SCHEDULE.update(
    {
        "reports-evaluate-schedules": {
            "task": "reports.evaluate_schedules",
            "schedule": crontab(minute="*/5"),
        },
        "reports-validate-definitions": {
            "task": "reports.validate_definitions",
            "schedule": crontab(minute=15, hour=21),
        },
        "reports-purge-files": {"task": "reports.purge_files", "schedule": crontab(minute=33)},
    }
)

# Epic 38: talent pools tick every 15 minutes (queue ``maintenance``): tenant-local nightly refresh + segment repair
# check, the daily new-member digest, and Pools marked due by a skills-graph change (refreshed within the hour).
BEAT_SCHEDULE.update(
    {"talent-pools-tick": {"task": "talent_pools.tick_all", "schedule": crontab(minute="*/15")}}
)

# Epic 42: referrals -- referee link expiry (06:30 IST = 01:00 UTC) and the daily reward evaluator (06:45 IST =
# 01:15 UTC), queue ``maintenance``.
BEAT_SCHEDULE.update(
    {
        "referrals-expire": {"task": "referrals.expire_all", "schedule": crontab(minute=0, hour=1)},
        "referrals-evaluate-rewards": {
            "task": "referrals.evaluate_rewards_all",
            "schedule": crontab(minute=15, hour=1),
        },
    }
)

# Epic 48: predictive analytics -- nightly per-tenant training + scoring fan-out at 01:00 IST (= 19:30 UTC, queue
# ``analytics``; runs that would start after 05:00 IST are deferred) and the daily snapshot purge (03:40 IST = 22:10
# UTC, queue ``maintenance``).
BEAT_SCHEDULE.update(
    {
        "analytics-nightly": {"task": "analytics.nightly", "schedule": crontab(minute=30, hour=19)},
        "analytics-purge-snapshots": {
            "task": "analytics.purge_snapshots",
            "schedule": crontab(minute=10, hour=22),
        },
    }
)

# Epic 41: internal mobility -- nightly board projection reconcile (02:20 IST = 20:50 UTC, queue ``maintenance``).
BEAT_SCHEDULE.update(
    {
        "mobility-reconcile-openings": {
            "task": "mobility.reconcile_openings_all",
            "schedule": crontab(minute=50, hour=20),
        }
    }
)

# Epic 39: recruitment marketing (queue ``campaigns``) -- the minute tick (due campaigns, deferred releases, sequence
# advance, A/B decisions, completion), the 15-minute reputation guard and the hourly dormant scan (once per tenant day).
BEAT_SCHEDULE.update(
    {
        "campaigns-tick": {"task": "campaigns.tick_all", "schedule": crontab(minute="*")},
        "campaigns-reputation": {"task": "campaigns.reputation_all", "schedule": crontab(minute="*/15")},
        "campaigns-dormant": {"task": "campaigns.dormant_all", "schedule": crontab(minute=5)},
    }
)

# Epic 49: bias audit -- daily publication-due reminder fan-out (07:15 IST = 01:45 UTC, queue ``maintenance``).
BEAT_SCHEDULE.update(
    {
        "fairness-publication-reminders": {
            "task": "fairness.publication_reminders",
            "schedule": crontab(minute=45, hour=1),
        }
    }
)

# Epic 43: campus & events -- hourly tick at :30 (queue ``maintenance``): expire import invites (minimised) and, at
# 03:30 UTC (09:00 IST), the daily ``campus_registration_received`` digest.
BEAT_SCHEDULE.update({"campus-tick": {"task": "campus.tick_all", "schedule": crontab(minute=30)}})

# Epic 53 (AD-58): agent runtime -- reaper every minute, proposal expiry every 15 minutes, budget alerts hourly, payload
# purge daily (02:40 UTC) and schedule triggers every minute (queue ``maintenance``).
BEAT_SCHEDULE.update(
    {
        "agents-reap-stalled": {"task": "agents.reap_stalled", "schedule": 60.0},
        "agents-expire-proposals": {"task": "agents.expire_proposals", "schedule": 900.0},
        "agents-budget-alerts": {"task": "agents.budget_alerts", "schedule": 3600.0},
        "agents-purge-payloads": {"task": "agents.purge_payloads", "schedule": crontab(minute=40, hour=2)},
        "agents-fire-schedules": {"task": "agents.fire_schedules", "schedule": crontab(minute="*")},
    }
)

# Epic 64: analytics answers -- daily purge of expired / orphaned answers (02:50 UTC, queue ``maintenance``).
BEAT_SCHEDULE.update(
    {"analytics-answers-purge": {"task": "analytics_answers.purge_expired", "schedule": crontab(minute=50, hour=2)}}
)

# Epic 55: Demand Agent -- connected-mailbox intake poll every 2 minutes (queue ``connectors``), the 180-day raw content
# purge (02:55 UTC) and flag expiry (03:05 UTC) on ``maintenance``. The daily SLA scan is an agent schedule trigger.
BEAT_SCHEDULE.update(
    {
        "demand-intake-poll-mailboxes": {"task": "demand_intake.poll_mailboxes", "schedule": 120.0},
        "demand-intake-purge-expired": {"task": "demand_intake.purge_expired", "schedule": crontab(minute=55, hour=2)},
        "demand-insights-expire-flags": {"task": "demand_insights.expire_flags", "schedule": crontab(minute=5, hour=3)},
    }
)

# Epic 56: Sourcing Agent -- weekly refresh trigger + superseded-shortlist purge, Mondays 03:30 UTC (queue ``maintenance``).
BEAT_SCHEDULE.update(
    {
        "sourcing-agent-weekly-refresh": {
            "task": "sourcing_agent.weekly_refresh",
            "schedule": crontab(minute=30, hour=3, day_of_week=1),
        }
    }
)

# Epic 54: Copilot -- daily purge of conversations past the tenant retention (02:50 UTC, queue ``maintenance``).
BEAT_SCHEDULE.update({"copilot-purge-expired": {"task": "copilot.purge_expired", "schedule": crontab(minute=50, hour=2)}})

# Epic 77: portal Copilot -- daily purge of conversations past ``expires_at`` (03:05 UTC, queue ``maintenance``).
BEAT_SCHEDULE.update({"portal-copilot-purge-expired": {"task": "portal_copilot.purge_expired", "schedule": crontab(minute=5, hour=3)}})

# Epic 57: Screening Agent -- reminders and expiry of AI screening conversations every 15 minutes (queue ``maintenance``).
BEAT_SCHEDULE.update({"screening-agent-sweep": {"task": "screening_agent.sweep", "schedule": 900.0}})

# Epic 59: Scheduling Agent -- due reminders / scorecard nudges -> reminder runs, link expiry, reminder pruning every
# 5 minutes (queue ``maintenance``).
BEAT_SCHEDULE.update(
    {"scheduling-agent-dispatch-reminders": {"task": "scheduling_agent.dispatch_reminders", "schedule": 300.0}}
)

# Epic 60: Interview Assistant -- hourly withdrawal sweep (24 h SLA for recordings / transcripts after a withdrawal),
# at :20 (queue ``maintenance``).
BEAT_SCHEDULE.update({"interview-captures-sweep": {"task": "interview_captures.sweep", "schedule": crontab(minute=20)}})

# Epic 62: Knowledge chunk reconcile every 5 minutes; Compliance / BGV chase ticks hourly at :05 / :10 (each acts at the
# tenant's local run hour; queue ``maintenance``).
BEAT_SCHEDULE.update(
    {
        "knowledge-reconcile": {"task": "knowledge.reconcile", "schedule": 300.0},
        "chasing-compliance-tick": {"task": "chasing.compliance_tick", "schedule": crontab(minute=5)},
        "chasing-bgv-tick": {"task": "chasing.bgv_tick", "schedule": crontab(minute=10)},
    }
)

# Epic 58: Voice Screening -- hourly withdrawal sweep (24 h SLA for recordings / transcripts after a withdrawal), at :40
# (queue ``maintenance``).
BEAT_SCHEDULE.update({"voice-sweep": {"task": "voice.sweep", "schedule": crontab(minute=40)}})

# Epic 63: Timesheet nudges (:15, act at 10:00 local), collections daily scan (:35, 10:30 local) and billing preparation
# (:45, the 1st at 09:00 local) -- hourly deterministic run starters on ``maintenance``. The P2 time / billing /
# receivables beats are unchanged.
BEAT_SCHEDULE.update(
    {
        "timesheet-flags-nudge-tick": {"task": "timesheet_flags.nudge_tick", "schedule": crontab(minute=15)},
        "collection-rules-daily-tick": {"task": "collection_rules.daily_tick", "schedule": crontab(minute=35)},
        "collection-rules-billing-prep-tick": {"task": "collection_rules.billing_prep_tick", "schedule": crontab(minute=45)},
    }
)

# Epic 65: weekly live evaluation run (Sundays 01:00 UTC, queue ``maintenance``; runs only where
# ``AI_EVALS_LIVE_ENABLED`` and ``ANTHROPIC_API_KEY_LIVE`` are set).
BEAT_SCHEDULE.update(
    {"ai-evals-schedule-live": {"task": "ai_evals.schedule_live", "schedule": crontab(minute=0, hour=1, day_of_week=0)}}
)

# Epic 66 (AD-78, NFR-71/74): usage metering flush every 60 s, daily meter reconciliation (00:30 UTC),
# monthly partition upkeep (01:10 UTC) and the 30-day request-log purge (02:50 UTC) -- queue ``maintenance``.
BEAT_SCHEDULE.update(
    {
        "metering-flush": {"task": "metering.flush", "schedule": 60.0},
        "metering-reconcile": {"task": "metering.reconcile", "schedule": crontab(minute=30, hour=0)},
        "partitions-ensure-monthly": {
            "task": "partitions.ensure_monthly",
            "schedule": crontab(minute=10, hour=1),
        },
        "api-request-log-purge": {"task": "api_request_log.purge", "schedule": crontab(minute=50, hour=2)},
    }
)

# Epic 66 (AD-79): outbox relay crash recovery every 30 s (queue ``webhooks``), daily outbox purge at 02:20 UTC and the
# subject-exposure buffer flush every 60 s (queue ``maintenance``).
BEAT_SCHEDULE.update(
    {
        "public-events-sweep": {"task": "public_events.sweep", "schedule": 30.0},
        "public-events-purge": {"task": "public_events.purge", "schedule": crontab(minute=20, hour=2)},
        "exposure-flush": {"task": "exposure.flush", "schedule": 60.0},
    }
)

# Epic 66 (§0.15): stop MCP (``origin='external'``) agent sessions idle > 30 minutes, every 5 minutes (queue
# ``maintenance``).
BEAT_SCHEDULE.update(
    {"agents-close-idle-external": {"task": "agents.close_idle_external", "schedule": 300.0}}
)

# Epic 66 (AD-81 / AD-76): connector schedules every 60 s (queue ``ecosystem``); hourly purge of expired OAuth token
# lookups and daily api_request_log purge (queue ``maintenance``).
BEAT_SCHEDULE.update(
    {
        "integrations-fire-schedules": {"task": "integrations.fire_schedules", "schedule": 60.0},
        "oauth-purge-lookups": {"task": "oauth.purge_lookups", "schedule": crontab(minute=7)},
    }
)

# Epic 67 (FR-347): the monthly deprecated-call report (daily 03:20 UTC; the task runs only on
# ``public_api_deprecation_report_day``) -- queue ``maintenance``.
BEAT_SCHEDULE.update(
    {"public-api-deprecation-report": {"task": "public_api.deprecation_report", "schedule": crontab(minute=20, hour=3)}}
)

# Epic 68 (AD-79, NFR-66): the per-tenant fairness pump every 5 s and the health scan every 5 min (queue
# ``webhooks``); the webhook retention purge daily at 02:35 UTC (queue ``maintenance``).
BEAT_SCHEDULE.update(
    {
        "webhooks-pump-all": {"task": "webhooks.pump_all", "schedule": 5.0},
        "webhooks-health-scan": {"task": "webhooks.health_scan", "schedule": 300.0},
        "webhooks-purge": {"task": "webhooks.purge", "schedule": crontab(minute=35, hour=2)},
    }
)

# Epic 78: unused / discarded AI campaign copy drafts purged daily at 02:45 UTC; the onboarding Q&A sealing backfill
# every 15 minutes until every tenant reports zero plaintext rows (then a no-op) -- queue ``maintenance``.
BEAT_SCHEDULE.update(
    {
        "campaign-copy-purge": {"task": "campaign_copy.purge", "schedule": crontab(minute=45, hour=2)},
        "knowledge-seal-backfill": {"task": "knowledge.seal_backfill", "schedule": 900.0},
    }
)

# Epic 76: provider recording deletes retried hourly (up to 72 h) and stuck calls reconciled every 15 min -- queue
# ``maintenance``.
BEAT_SCHEDULE.update(
    {
        "telephony-retry-provider-deletes": {"task": "telephony.retry_provider_deletes", "schedule": crontab(minute=25)},
        "telephony-reconcile-calls": {"task": "telephony.reconcile_calls", "schedule": 900.0},
    }
)

# Epic 73: MCP session mirror (idle runs) + hold auto-release every 5 min, metadata purge daily 02:55 UTC -- queue
# ``maintenance``.
BEAT_SCHEDULE.update(
    {
        "mcp-detector-rollup": {"task": "mcp.detector_rollup", "schedule": 300.0},
        "mcp-purge": {"task": "mcp.purge", "schedule": crontab(minute=55, hour=2)},
    }
)

# Epic 69 (Integration Marketplace): hourly connector health sweep (queue ``ecosystem``), the manifest -> listing sync
# daily at 02:40 IST (21:10 UTC) and the aggregate install counts daily (queue ``maintenance``).
BEAT_SCHEDULE.update(
    {
        "marketplace-health-sweep": {"task": "marketplace.health_sweep", "schedule": crontab(minute=17)},
        "marketplace-sync-catalog": {"task": "marketplace.sync_platform_catalog", "schedule": crontab(minute=10, hour=21)},
        "marketplace-install-counts": {"task": "marketplace.refresh_install_counts", "schedule": crontab(minute=40, hour=21)},
    }
)

# Epic 74 (Developer Portal): unverified-account purge + invitation expiry + Verified Partner expiry daily, stale
# conformance runs every 10 min -- queue ``maintenance``.
BEAT_SCHEDULE.update(
    {
        "developer-purge-unverified": {"task": "developer.purge_unverified", "schedule": crontab(minute=5, hour=22)},
        "developer-expire-invitations": {"task": "developer.expire_invitations", "schedule": crontab(minute=15, hour=22)},
        "developer-expire-verifications": {"task": "developer.expire_verifications", "schedule": crontab(minute=25, hour=22)},
        "developer-expire-conformance-runs": {"task": "developer.expire_conformance_runs", "schedule": 600.0},
    }
)

# Epic 70: job board expiry scan daily 08:30 IST (03:00 UTC) and the applicant digest daily 09:00 IST (03:30 UTC) --
# queue ``ecosystem``.
BEAT_SCHEDULE.update(
    {
        "job-distribution-expiry-scan": {"task": "job_distribution.expiry_scan", "schedule": crontab(minute=0, hour=3)},
        "job-distribution-applicant-digest": {"task": "job_distribution.applicant_digest", "schedule": crontab(minute=30, hour=3)},
    }
)

# Epic 72 (Calendar Sync): push channel renewal every 15 min and the fallback / safety-net delta poll every minute
# (queue ``ecosystem``).
BEAT_SCHEDULE.update(
    {
        "calendar-sync-renew-due": {"task": "calendar_sync.renew_due_all", "schedule": 900.0},
        "calendar-sync-poll-due": {"task": "calendar_sync.poll_due_all", "schedule": 60.0},
    }
)

# Epic 75 (Job Network): index drift reconcile every 10 min, stats flush every 5 min, daily purge of reports / unlisted
# rows at 04:40 UTC (queue ``maintenance``).
BEAT_SCHEDULE.update(
    {
        "job-network-reconcile": {"task": "job_network.reconcile", "schedule": 600.0},
        "job-network-flush-stats": {"task": "job_network.flush_stats", "schedule": 300.0},
        "job-network-purge": {"task": "job_network.purge", "schedule": crontab(minute=40, hour=4)},
    }
)

# Epic 79 (Phase 5 Governance): open access reviews when the cadence is due + auto-revoke stale items daily 05:10 UTC,
# and the inactive-sandbox purge daily 05:40 UTC (queue ``maintenance``).
BEAT_SCHEDULE.update(
    {
        "ecosystem-governance-open-reviews": {
            "task": "ecosystem_governance.open_reviews",
            "schedule": crontab(minute=10, hour=5),
        },
        "ecosystem-governance-sandbox-purge": {
            "task": "ecosystem_governance.sandbox_purge",
            "schedule": crontab(minute=40, hour=5),
        },
    }
)
