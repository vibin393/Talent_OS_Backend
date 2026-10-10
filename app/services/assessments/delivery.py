# ruff: noqa: E501
"""Delivery serialiser (Epic 40 A6, NFR-35): what a Candidate sees of an item, and nothing more.

``DeliveryItem = {position, section_key, kind, stem, options (shuffled, opaque per-attempt ids), code?, language?,
lines? (shuffled for code_order), blanks? (count only), points}``. NEVER included: ``answer_key``, ``rubric``,
accepted answers, regexes, keywords, bug lines, the correct order, item ids or item-version ids.

The attempt's ``section_plan`` keeps, per position, the item version id and the opaque -> original option / line id
maps; ``to_original`` maps a Candidate response back before scoring. Responses are validated per kind
(``extra='forbid'``) and capped at 20 KB.
"""

import json
import random
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.models.assessments import AssessmentItemVersion
from app.schemas.assessments_delivery import ChoiceOut, DeliveryItem
from app.services.assessments import draw as draw_mod
from app.services.assessments.common import MAX_ANSWER_BYTES, invalid
from app.services.assessments.items import BLANK_RE


def plan_entry(
    position: int,
    kind: str,
    version: AssessmentItemVersion,
    points: Decimal,
    shuffle_options: bool,
    rng: random.Random,
) -> dict[str, Any]:
    """One ``section_plan`` entry: opaque per-attempt ids for options / lines (shuffled as configured)."""
    entry: dict[str, Any] = {
        "position": position,
        "item_version_id": str(version.id),
        "kind": kind,
        "points": str(points),
    }
    choices = (version.options or {}).get("choices") or []
    if choices:
        order = draw_mod.shuffled(choices, rng) if shuffle_options and kind != "true_false" else list(choices)
        ids = draw_mod.opaque_ids(len(order), rng, "o")
        entry["opt"] = [[oid, c["id"]] for oid, c in zip(ids, order, strict=True)]
    lines = (version.options or {}).get("lines") or []
    if lines:
        order = draw_mod.shuffled(lines, rng)  # code_order: always shuffled (the stored order is the answer)
        if [line["id"] for line in order] == [line["id"] for line in lines] and len(lines) > 1:
            order = order[1:] + order[:1]
        ids = draw_mod.opaque_ids(len(order), rng, "l")
        entry["lines"] = [[lid, line["id"]] for lid, line in zip(ids, order, strict=True)]
    return entry


def delivery_item(entry: dict[str, Any], section_key: str, version: AssessmentItemVersion) -> DeliveryItem:
    kind = entry["kind"]
    texts = {c["id"]: c["text"] for c in (version.options or {}).get("choices") or []}
    line_texts = {line["id"]: line["text"] for line in (version.options or {}).get("lines") or []}
    return DeliveryItem(
        position=int(entry["position"]),
        section_key=section_key,
        kind=kind,
        stem=version.stem,
        options=[ChoiceOut(id=oid, text=texts.get(orig, "")) for oid, orig in entry.get("opt") or []] or None,
        code=version.code if kind != "code_order" else None,
        language=version.language,
        lines=[ChoiceOut(id=lid, text=line_texts.get(orig, "")) for lid, orig in entry.get("lines") or []]
        or None,
        blanks=len(set(BLANK_RE.findall(version.code or ""))) if kind == "code_fill" else None,
        points=Decimal(str(entry["points"])),
    )


def to_original(entry: dict[str, Any], response: dict[str, Any] | None) -> dict[str, Any] | None:
    """Map the opaque option / line ids of a response back to the item's own ids (unknown ids are dropped)."""
    if not isinstance(response, dict):
        return None
    out = dict(response)
    opt = dict(entry.get("opt") or [])
    lines = dict(entry.get("lines") or [])
    if "choice" in out:
        out["choice"] = opt.get(out["choice"]) if isinstance(out["choice"], str) else None
    if isinstance(out.get("choices"), list):
        out["choices"] = [opt[c] for c in out["choices"] if isinstance(c, str) and c in opt]
    if isinstance(out.get("order"), list):
        out["order"] = [lines[c] for c in out["order"] if isinstance(c, str) and c in lines]
    return out


# ---------------------------------------------------------------- response validation (candidate input)
class _R(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChoiceResp(_R):
    choice: Annotated[str, Field(max_length=40)] | None = None


class MultiResp(_R):
    choices: Annotated[list[Annotated[str, Field(max_length=40)]], Field(max_length=20)] = Field(
        default_factory=list
    )


class NumericResp(_R):
    value: Annotated[str, Field(max_length=60)] | None = None


class TextResp(_R):
    text: Annotated[str, Field(max_length=MAX_ANSWER_BYTES)] | None = None


class FillResp(_R):
    blanks: dict[Annotated[str, Field(pattern=r"^\d{1,2}$")], Annotated[str, Field(max_length=500)]] = Field(
        default_factory=dict
    )


class OrderResp(_R):
    order: Annotated[list[Annotated[str, Field(max_length=40)]], Field(max_length=100)] = Field(
        default_factory=list
    )


class LinesResp(_R):
    lines: Annotated[list[Annotated[int, Field(ge=1, le=5000)]], Field(max_length=200)] = Field(
        default_factory=list
    )


RESPONSE_MODEL: dict[str, type[_R]] = {
    "single": ChoiceResp, "true_false": ChoiceResp, "multi": MultiResp, "numeric": NumericResp, "short_text": TextResp,
    "code_output": TextResp, "code_freeform": TextResp, "essay": TextResp, "code_fill": FillResp, "code_order": OrderResp,
    "code_find_bug": LinesResp,
}  # fmt: skip


def validate_response(kind: str, response: dict[str, Any] | None, position: int) -> dict[str, Any] | None:
    if response is None:
        return None
    if len(json.dumps(response, default=str).encode()) > MAX_ANSWER_BYTES:
        raise invalid("answer_too_large", "An answer is larger than 20 KB", f"answers[{position}]")
    try:
        parsed = RESPONSE_MODEL[kind].model_validate(response)
    except (ValidationError, KeyError) as exc:
        raise invalid(
            "invalid_answer", "The answer does not match the question type", f"answers[{position}]"
        ) from exc
    return parsed.model_dump(exclude_none=True)


__all__ = ["delivery_item", "plan_entry", "to_original", "validate_response"]
