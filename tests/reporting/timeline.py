"""The fixed March 2026 timeline (IST) behind the manager / account / link tests. Hand-computed expectations
are listed in ``EXPECTED`` (range 2026-03-01 .. 2026-03-31, the manager's BU A).

Demands (BU A unless noted; D1-D3 owned by the account manager on client C1):
  D1 opened 03-02 10:00, 2 positions, filled   D2 opened 03-05 10:00, open (SLA at risk)
  D3 opened 03-10 10:00, open (SLA breached)    D4 BU B, opened 03-03, open (SLA at risk)
  D5 opened 02-01 10:00, closed (filled 03-15), owner admin      D6 client C2 (admin's), owner account manager, open
Applications (owner: recruiter unless noted):
  a1 cand1 on D1 created 03-02 12:00 applied > shortlisted > submitted > interviewing > selected > offered >
     hired 03-08 10:00 (source search)
  a2 cand2 on D1 created 03-04 10:00 > shortlisted > submitted > interviewing > hired 03-12 10:00 (manual)
  a3 cand2 on D2 created 03-05 12:00 > shortlisted > submitted (manual)
  a4 cand3 on D2 created 03-06 12:00 > phone_screen (custom) > shortlisted > submitted (career_portal)
  a5 cand4 on D3 created 03-11 12:00 applied (career_portal)
  a6 cand4 on D5 created 02-15 12:00 > hired 03-15 10:00 (manual)
  a7 cand5 on D4 created 03-03 12:00 (owner recruiter B) > shortlisted > submitted
Submissions (sent by the recruiter unless noted):
  s1 a1 03-02 20:00   s2 a2 03-05 11:30 (feedback interested)   s3 a3 03-06 16:00
  s6 a4 03-09 00:30 IST (= 03-08 19:00 UTC: IST-midnight bucketing)   s4 a7 03-04 (recruiter B, BU B)
  s5 a4 draft (never sent)
Interviews: i1 a1 03-04 15:00 completed (organiser recruiter); i2 a3 03-09 cancelled.
Offers: o1 a1 accepted, released 03-07 10:00; o2 a3 viewed, released 03-20; o3 a4 released 03-21.
"""

import dataclasses
import uuid
from typing import Any

from tests.reporting.helpers import ROrg, Seed, ist

RANGE = {"from": "2026-03-01", "to": "2026-03-31"}

EXPECTED: dict[str, Any] = {
    "open_demands": 3,  # D2, D3, D6
    "sla_at_risk": 1,  # D2 (D5's clock completed, D4 out of BU)
    "sla_breached": 1,  # D3
    "ttfs_median_hours": 20.0,  # D1 10 h, D2 30 h (D3 none): even n
    "ttfs_n": 2,
    "ttf_median_days": 26.0,  # D1 (2 positions) 10 days, D5 42 days
    "ttf_n": 2,
    "db_sourced_pct": 50.0,  # s1, s3 of s1, s2, s3, s6
    "sub_to_interview_pct": 25.0,  # s1 of 4
    "submissions_by_day": {"2026-03-02": 1, "2026-03-05": 1, "2026-03-06": 1, "2026-03-09": 1},
    "interviews_by_day": {"2026-03-04": 1},
    "offers_by_day": {"2026-03-07": 1, "2026-03-20": 1, "2026-03-21": 1},
    "hires_by_day": {"2026-03-08": 1, "2026-03-12": 1, "2026-03-15": 1},
    "funnel": [
        ("applied", 5, None),
        ("shortlisted", 4, 80.0),
        ("submitted", 4, 100.0),
        ("interviewing", 2, 50.0),
        ("selected", 1, 50.0),
        ("offered", 1, 100.0),
        ("hired", 2, 200.0),
    ],
    "sources": {
        "career_portal": {"applications": 2, "submissions": 1, "interviews": 0, "hires": 0, "hire_rate": 0.0},
        "manual": {"applications": 2, "submissions": 2, "interviews": 0, "hires": 1, "hire_rate": 50.0},
        "search": {"applications": 1, "submissions": 1, "interviews": 1, "hires": 1, "hire_rate": 100.0},
    },
    "activity_recruiter": {
        "applications_added": 5,
        "screenings_completed": 1,
        "submissions_sent": 4,
        "notes_logged": 2,
        "emails_sent": 0,
        "interviews_scheduled": 1,
    },
    # Account manager (clients owned: C1 -> D1, D2, D3)
    "client_row": {
        "open": 2,
        "on_hold": 0,
        "filled": 1,
        "positions": 4,
        "hires": 2,
        "fill_progress": 50.0,
        "submissions": 4,
        "interviews": 1,
    },
    "awaiting_feedback_account": 3,  # s1, s3, s6 (s2 has feedback)
    "offers_pending_account": 2,  # o2 viewed, o3 released
}


@dataclasses.dataclass
class March:
    c1: uuid.UUID
    c2: uuid.UUID
    demands: dict[str, uuid.UUID]
    apps: dict[str, uuid.UUID]
    subs: dict[str, uuid.UUID]


async def build_march(org: ROrg) -> March:
    rec, rec_b, acct, admin = (
        org.recruiter.user_id,
        org.recruiter_b.user_id,
        org.account.user_id,
        org.admin.user_id,
    )
    async with Seed(org.tenant_id) as s:
        await s.custom_stage("phone_screen", 15)
        c1 = await s.client(acct, "Acme Corp")
        c2 = await s.client(admin, "Globex")
        d: dict[str, uuid.UUID] = {}
        d["D1"] = await s.demand(
            c1, acct, status="filled", opened_at=ist(2026, 3, 2, 10), positions=2, bu=org.bu_a
        )
        d["D2"] = await s.demand(c1, acct, opened_at=ist(2026, 3, 5, 10), bu=org.bu_a)
        d["D3"] = await s.demand(c1, acct, opened_at=ist(2026, 3, 10, 10), bu=org.bu_a)
        d["D4"] = await s.demand(c2, admin, opened_at=ist(2026, 3, 3, 10), bu=org.bu_b)
        d["D5"] = await s.demand(c2, admin, status="closed", opened_at=ist(2026, 2, 1, 10), bu=org.bu_a)
        d["D6"] = await s.demand(c2, acct, opened_at=ist(2026, 3, 4, 10), bu=org.bu_a)
        await s.sla_clock(d["D2"], "at_risk")
        await s.sla_clock(d["D3"], "breached")
        await s.sla_clock(d["D4"], "at_risk")
        await s.sla_clock(d["D5"], "at_risk", completed=True)
        for key in ("D1", "D2", "D3"):
            await s.assign(d[key], rec)
        cand1 = await s.candidate(rec, ist(2026, 1, 1), "Asha Iyer")
        cand2 = await s.candidate(rec, ist(2026, 3, 4, 9), "Bhavna Rao")
        cand3 = await s.candidate(rec, ist(2026, 3, 6, 11), "Chitra Das")
        cand4 = await s.candidate(rec, ist(2026, 3, 1), "Deepa Nair")
        cand5 = await s.candidate(rec_b, ist(2026, 2, 1), "Esha Menon")
        a: dict[str, uuid.UUID] = {}
        a["a1"] = await s.application(
            cand1,
            d["D1"],
            rec,
            ist(2026, 3, 2, 12),
            source="search",
            path=[
                ("shortlisted", ist(2026, 3, 2, 13)),
                ("submitted", ist(2026, 3, 2, 20)),
                ("interviewing", ist(2026, 3, 4, 10)),
                ("selected", ist(2026, 3, 6, 10)),
                ("offered", ist(2026, 3, 7, 10)),
                ("hired", ist(2026, 3, 8, 10)),
            ],
        )
        a["a2"] = await s.application(
            cand2,
            d["D1"],
            rec,
            ist(2026, 3, 4, 10),
            path=[
                ("shortlisted", ist(2026, 3, 4, 11)),
                ("submitted", ist(2026, 3, 5, 11, 30)),
                ("interviewing", ist(2026, 3, 6, 10)),
                ("hired", ist(2026, 3, 12, 10)),
            ],
        )
        a["a3"] = await s.application(
            cand2,
            d["D2"],
            rec,
            ist(2026, 3, 5, 12),
            path=[("shortlisted", ist(2026, 3, 5, 13)), ("submitted", ist(2026, 3, 6, 16))],
        )
        a["a4"] = await s.application(
            cand3,
            d["D2"],
            rec,
            ist(2026, 3, 6, 12),
            source="career_portal",
            path=[
                ("phone_screen", ist(2026, 3, 7, 10)),
                ("shortlisted", ist(2026, 3, 8, 10)),
                ("submitted", ist(2026, 3, 9, 0, 30)),
            ],
        )
        a["a5"] = await s.application(cand4, d["D3"], rec, ist(2026, 3, 11, 12), source="career_portal")
        a["a6"] = await s.application(
            cand4, d["D5"], rec, ist(2026, 2, 15, 12), path=[("hired", ist(2026, 3, 15, 10))]
        )
        a["a7"] = await s.application(
            cand5,
            d["D4"],
            rec_b,
            ist(2026, 3, 3, 12),
            path=[("shortlisted", ist(2026, 3, 3, 13)), ("submitted", ist(2026, 3, 4, 12))],
        )
        subs: dict[str, uuid.UUID] = {}
        subs["s1"] = await s.submission(a["a1"], sent_at=ist(2026, 3, 2, 20), sent_by=rec)
        subs["s2"] = await s.submission(
            a["a2"], sent_at=ist(2026, 3, 5, 11, 30), sent_by=rec, feedback="interested"
        )
        subs["s3"] = await s.submission(a["a3"], sent_at=ist(2026, 3, 6, 16), sent_by=rec)
        subs["s6"] = await s.submission(a["a4"], sent_at=ist(2026, 3, 9, 0, 30), sent_by=rec)
        subs["s4"] = await s.submission(a["a7"], sent_at=ist(2026, 3, 4, 12), sent_by=rec_b)
        subs["s5"] = await s.submission(a["a4"], sent_at=None, sent_by=None, status="draft")
        await s.interview(a["a1"], ist(2026, 3, 4, 15), rec, status="completed", participants=(rec,))
        await s.interview(a["a3"], ist(2026, 3, 9, 15), rec, status="cancelled", participants=(rec,))
        await s.offer(a["a1"], rec, status="accepted", released_at=ist(2026, 3, 7, 10))
        await s.offer(a["a3"], rec, status="viewed", released_at=ist(2026, 3, 20, 10))
        await s.offer(a["a4"], rec, status="released", released_at=ist(2026, 3, 21, 10))
        await s.screening(a["a3"], rec, ist(2026, 3, 10, 10))
        await s.activity(rec, "note", ist(2026, 3, 10, 11), d["D2"])
        await s.activity(rec, "call", ist(2026, 3, 11, 11), d["D2"])
        await s.activity(rec, "system", ist(2026, 3, 11, 12), d["D2"])
        await s.activity(rec, "note", ist(2026, 4, 2, 11), d["D2"])  # out of range
    return March(c1=c1, c2=c2, demands=d, apps=a, subs=subs)
