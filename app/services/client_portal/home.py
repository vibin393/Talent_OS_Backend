"""Client Portal home tiles (Epic 18 A5): approvals count, new sent submissions (7 days), upcoming interview
dates, team size / ending within 30 days and issued invoices (Epic 29 ``billing.portal``). A tile is omitted when
the Client User lacks its portal permission."""

from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.client_hub_portal import ClientHomeOut, HomeTeamTile
from app.services.client_portal_base import has_permission


async def home(ctx: RequestContext, db: AsyncSession) -> ClientHomeOut:
    from app.services.client_portal import approvals

    out = ClientHomeOut(display_name=str(getattr(ctx.principal, "display_name", None) or "there"))
    out.approvals_pending = await approvals.pending_count(ctx, db)
    if has_permission(ctx, "submissions:review"):
        from app.services.interviews import portal as interviews_portal
        from app.services.pipeline import portal as pipeline_portal

        since = datetime.now(UTC) - timedelta(days=7)
        subs = await pipeline_portal.client_list_submissions(ctx, db)
        out.new_submissions = sum(
            1 for s in subs if s.sent_at and s.sent_at >= since and s.feedback_status == "none"
        )
        upcoming = await interviews_portal.client_upcoming_interviews(ctx, db)
        out.upcoming_interview_dates = sorted({i.starts_at.date() for i in upcoming})
    if has_permission(ctx, "placements:view"):
        from app.services.placements import portal as placements_portal

        team = [
            p
            for p in await placements_portal.client_placements(ctx, db)
            if p.status in ("active", "pre_joining")
        ]
        out.team = HomeTeamTile(size=len(team), ending_within_30_days=sum(1 for p in team if p.ending_soon))
    if has_permission(ctx, "invoices:view"):
        try:
            from app.services.billing import portal as billing_portal

            docs = await billing_portal.client_list(ctx, db)
            out.invoices_issued = sum(
                1
                for d in docs
                if getattr(d, "kind", "invoice") == "invoice" and getattr(d, "status", "") != "cancelled"
            )
        except Exception:
            logger.exception("Client home: invoices unavailable")
    return out
