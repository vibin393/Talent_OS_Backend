# ruff: file-ignore[line-too-long]
"""Assessment results in Submissions (Epic 40 A9, FR-208).

The Recruiter chooses, per Submission, which results to include (``PUT /submissions/{id}/assessment-shares``; only
``reviewed|passed|failed`` invitations of the Submission's Candidate). Shared results are attached to the submission
email as a one-page PDF summary (score, percentile, sections, skills; never answers, never proctoring signals) via the
P1 hook ``pipeline.submissions.register_submission_attachment_provider``. Unshared results never reach a Client.
"""

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.assessments import Assessment, AssessmentInvitation, SubmissionAssessmentShare
from app.schemas.assessments import SharesIn, SharesOut
from app.services.assessments.common import invalid, not_found, now, user_id

SHAREABLE = ("reviewed", "passed", "failed")


async def _submission(db: AsyncSession, submission_id: uuid.UUID) -> Any:
    from app.models.pipeline import Submission

    sub = await db.get(Submission, submission_id)
    if sub is None:
        raise not_found("Submission")
    return sub


async def get_shares(ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID) -> SharesOut:  # ruff: ignore[unused-function-argument]
    await _submission(db, submission_id)
    rows = (
        await db.execute(
            select(SubmissionAssessmentShare.invitation_id).where(
                SubmissionAssessmentShare.submission_id == submission_id
            )
        )
    ).scalars()
    return SharesOut(invitation_ids=list(rows))


async def put_shares(
    ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID, data: SharesIn
) -> SharesOut:
    from app.models.pipeline import Application
    from app.services.pipeline import submissions as p_submissions

    sub = await _submission(db, submission_id)
    await p_submissions.get_submission(
        ctx, db, submission_id
    )  # the caller may read the submission (404 / 403)

    app = await db.get(Application, sub.application_id)
    candidate_id = app.candidate_id if app is not None else None
    wanted = list(dict.fromkeys(data.invitation_ids))
    if wanted:
        invs = list(
            (
                await db.execute(select(AssessmentInvitation).where(AssessmentInvitation.id.in_(wanted)))
            ).scalars()
        )
        if len(invs) != len(wanted) or any(
            i.candidate_id != candidate_id or i.status not in SHAREABLE for i in invs
        ):
            raise invalid(
                "share_not_allowed",
                "Only reviewed, passed or failed results of this candidate can be shared",
                "invitation_ids",
            )
    await db.execute(
        delete(SubmissionAssessmentShare).where(SubmissionAssessmentShare.submission_id == submission_id)
    )
    for inv_id in wanted:
        db.add(
            SubmissionAssessmentShare(
                id=uuid.uuid4(),
                tenant_id=sub.tenant_id,
                submission_id=submission_id,
                invitation_id=inv_id,
                created_by=user_id(ctx),
                created_at=now(),
            )
        )
    await db.flush()
    return SharesOut(invitation_ids=wanted)


def summary_markdown(name: str, inv: AssessmentInvitation) -> str:
    lines = [f"# {name}", "", f"**Score:** {inv.score if inv.score is not None else '-'}%"]
    if inv.percentile is not None:
        lines.append(f"**Percentile:** {inv.percentile}")
    if inv.result:
        lines.append(f"**Result:** {'Passed' if inv.result == 'pass' else 'Not passed'}")
    sections = inv.section_scores or {}
    if sections:
        lines += ["", "## Sections", "", "| Section | Score |", "|---|---|"]
        lines += [
            f"| {v.get('title', k)} | {v.get('pct', '-')}% |"
            for k, v in sections.items()
            if isinstance(v, dict)
        ]
    skills = inv.skill_scores or {}
    if skills:
        lines += ["", "## Skills", "", "| Skill | Score | Level |", "|---|---|---|"]
        lines += [
            f"| {v.get('label') or k} | {v.get('pct') or '-'}% | {v.get('proficiency') or '-'} |"
            for k, v in sorted(skills.items())
            if isinstance(v, dict)
        ]
    return "\n".join(lines)


async def submission_attachments(db: AsyncSession, sub: Any) -> list[Any]:
    """P1 submission hook: one PDF per shared result (summary only)."""
    from app.core.mailer import EmailAttachment
    from app.services.documents.render import render_markdown

    rows = (
        await db.execute(
            select(AssessmentInvitation, Assessment.name)
            .join(
                SubmissionAssessmentShare, SubmissionAssessmentShare.invitation_id == AssessmentInvitation.id
            )
            .join(Assessment, Assessment.id == AssessmentInvitation.assessment_id)
            .where(
                SubmissionAssessmentShare.submission_id == sub.id, AssessmentInvitation.status.in_(SHAREABLE)
            )
        )
    ).all()
    out: list[Any] = []
    for inv, name in rows:
        pdf = render_markdown(
            summary_markdown(name, inv),
            header=name,
            footer="Assessment result summary",
            title=f"{name} - result",
        )
        safe = "".join(c for c in name if c.isalnum() or c in " -_")[:60].strip() or "assessment"
        out.append(EmailAttachment(filename=f"{safe} result.pdf", content_type="application/pdf", data=pdf))
    return out


__all__ = ["get_shares", "put_shares", "submission_attachments", "summary_markdown"]
