"""Pipeline emails sent from Celery tasks (AD-9): the tenant rejection email."""

import uuid
from string import Formatter
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.mailer import EmailMessage, send_email
from app.models.pipeline import Application, PipelineStage
from app.services.pipeline.common import (
    DEFAULT_REJECTION_BODY,
    DEFAULT_REJECTION_SUBJECT,
    settings_row,
    timeline,
)


class _Safe(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _fill(template: str, values: dict[str, Any]) -> str:
    # Only simple ``{name}`` fields; attribute / index access in tenant templates is not interpreted.
    safe = "".join(
        literal + ("{" + field + "}" if field and field.isidentifier() else "")
        for literal, field, _spec, _conv in Formatter().parse(
            template.replace("{{", "\x00").replace("}}", "\x01")
        )
    )
    return safe.format_map(_Safe(values)).replace("\x00", "{").replace("\x01", "}")


async def send_rejection_email(ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID) -> bool:
    from app.services import identity_bridge
    from app.services.candidates import pipeline_port as candidates
    from app.services.demands import pipeline_port as demands_port

    app = await db.get(Application, application_id)
    if app is None:
        return False
    stage = await db.get(PipelineStage, app.stage_id)
    if stage is None or stage.key != "rejected":
        return False
    settings = await settings_row(db)
    if not settings.rejection_email_enabled:
        return False
    email = await candidates.primary_email(db, app.candidate_id)
    if email is None:
        return False
    cand = (await candidates.summaries(db, [app.candidate_id])).get(app.candidate_id, {})
    demand = await demands_port.get_row(db, app.demand_id)
    tenant = await identity_bridge.get_tenant(db, app.tenant_id)
    values = {
        "first_name": cand.get("first_name") or "there",
        "job_title": demand.title,
        "tenant_name": tenant.name if tenant else "",
    }
    await send_email(
        EmailMessage(
            to=email,
            subject=_fill(settings.rejection_email_subject or DEFAULT_REJECTION_SUBJECT, values),
            text=_fill(settings.rejection_email_body or DEFAULT_REJECTION_BODY, values),
            tenant_id=app.tenant_id,
            kind="rejection",
        )
    )
    await timeline(
        ctx,
        db,
        "candidate",
        app.candidate_id,
        f"{app.human_id}: rejection email sent",
        {"application_id": str(app.id)},
        kind="email",
    )
    return True
