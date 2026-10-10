# ruff: noqa: E501
"""AI explanations of predictions (Epic 48 A6, FR-244, NFR-44). Claude only writes the explanation.

``explain(ctx, db, prediction_id)`` (lazy, cached on the prediction row):

1. cached (``explanation_status='ok'``) -> returned as is;
2. ``prediction_explanation`` AI feature off -> ``unavailable`` (the UI shows the contributions table only);
3. otherwise ``ai.gateway.run`` with **only** the kind label, the score text, the band and the top-5 labelled
   contributions (``"<key> | <label>: <direction> <magnitude>"``). No names, no record text, no free text, no ids
   beyond the subject reference the gateway stores (``subject=(subject_kind, subject_id)``);
4. validation: ``cited_feature_keys`` must be a subset of the model spec keys and the text must not contain a
   denylisted token; otherwise the output is discarded -> ``unavailable``;
5. stores ``explanation_text`` / ``explanation_ai_action_id``. A gateway failure -> ``unavailable`` (not cached:
   the next request retries).
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.analytics import MlPrediction
from app.schemas.analytics import ExplanationOut, PredictionExplanation
from app.services import ai
from app.services.analytics import reads
from app.services.analytics.features import denylist
from app.services.analytics.features.specs import SPECS

FEATURE = "prediction_explanation"
PROMPT_ID = "prediction_explanation"
TOP_N = 5


def score_text(p: MlPrediction) -> str:
    if p.kind == "time_to_fill":
        interval = p.interval or {}
        return f"about {p.predicted_days} days to fill (likely range {interval.get('p20')} to {interval.get('p80')} days)"
    return f"probability {p.score}"


def prompt_inputs(p: MlPrediction) -> dict[str, str]:
    lines = [
        f"{c['key']} | {c['label']}: {'raises' if c['direction'] == 'up' else 'lowers'} {c['display']} (value: {c['value_display']})"
        for c in (p.contributions or [])[:TOP_N]
    ]
    return {
        "kind_label": SPECS[p.kind].kind_label,
        "score_text": score_text(p),
        "band": p.band or "not applicable",
        "contributions": "\n".join(lines) or "(none)",
    }


def valid(p: MlPrediction, out: PredictionExplanation) -> bool:
    keys = set(SPECS[p.kind].keys)
    if not set(out.cited_feature_keys) <= keys:
        return False
    if denylist.text_violations(out.text):
        return False
    return 0 < len(out.text.strip()) <= 400


async def explain(ctx: RequestContext, db: AsyncSession, prediction_id: uuid.UUID) -> ExplanationOut:
    p = await reads.readable_prediction(ctx, db, prediction_id)
    if p.explanation_status == "ok" and p.explanation_text:
        return ExplanationOut(status="ok", text=p.explanation_text)
    if not await ai.is_feature_enabled(db, FEATURE):
        return ExplanationOut(status="unavailable")
    result = await ai.run(
        ctx,
        db,
        feature=FEATURE,
        prompt_id=PROMPT_ID,
        inputs=prompt_inputs(p),
        untrusted={},
        output_schema=PredictionExplanation,
        subject=(p.subject_kind, p.subject_id),
    )
    data = result.data if result.ok else None
    if not isinstance(data, PredictionExplanation) or not valid(p, data):
        if result.ok:
            p.explanation_status = "unavailable"
            p.explanation_ai_action_id = result.action_id
            await db.flush()
        return ExplanationOut(status="unavailable")
    p.explanation_text = data.text.strip()
    p.explanation_ai_action_id = result.action_id
    p.explanation_status = "ok"
    await db.flush()
    return ExplanationOut(status="ok", text=p.explanation_text)
