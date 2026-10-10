# ruff: noqa: E501
"""Campaign copy evaluation set (Epic 78 A7, FR-401..FR-403).

Offline the Fake LLM "answer" of each case is the
``emit_result`` payload in the YAML; a case passes when

* the result validates against ``CampaignCopyResult`` exactly as expected (``expect.valid``), and
* golden cases (``tags: [golden]``) produce no ``block`` finding on any variant, and
* red-team cases (``tags: [redteam]``) produce every expected blocking code -- the model's mistake is caught by the
  deterministic checks, never silently "fixed".

Gate: overall score >= 0.9 and red-team 100 %. The scheduled live run (Epic 79.7) replaces the YAML answer with the
real model's output for the same brief and context.
"""

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.schemas.campaign_copy import CampaignCopyResult
from app.services.campaign_copy import checks

ROOT = Path(__file__).resolve().parent / "cases"
HOSTS = frozenset({"careers.example.test", "app.example.test"})
GATE = 0.9


def load(root: Path = ROOT) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.yaml")):
        out.extend(yaml.safe_load(path.read_text(encoding="utf-8")) or [])
    return out


def _context(case: dict[str, Any]) -> checks.CheckContext:
    postings = tuple(
        checks.PostingFacts(
            posting_id=str(p.get("posting_id", "p")), title=str(p.get("title", "")), location=p.get("location"),
            work_mode=p.get("work_mode"), show_rate=bool(p.get("show_rate", False)),
            pay_min=Decimal(str(p["pay_min"])) if p.get("pay_min") is not None else None,
            pay_max=Decimal(str(p["pay_max"])) if p.get("pay_max") is not None else None, description=str(p.get("description", "")),
        )
        for p in case.get("postings") or []
    )  # fmt: skip
    tpl = case.get("template")
    template = checks.TemplateFacts(tpl["body"], tuple(tpl["variables"]), tuple(tpl.get("url_variables") or ())) if tpl else None
    return checks.CheckContext(channel=case["channel"], postings=postings, brief_text=str(case.get("brief", "")), allowed_hosts=HOSTS, template=template)


def evaluate(case: dict[str, Any]) -> tuple[bool, str]:
    expect = case.get("expect") or {}
    try:
        result = CampaignCopyResult.model_validate(case["result"])
    except ValidationError:
        ok = expect.get("valid", True) is False
        return ok, "invalid result" if ok else "result failed schema validation"
    if expect.get("valid", True) is False:
        return False, "an invalid result was accepted"
    ctx = _context(case)
    found: set[str] = set()
    for seg in result.segments:
        for v in seg.variants:
            draft = {
                "subject": v.subject, "preheader": v.preheader, "body_html": v.body_html,
                "template_values": v.template_values or {}, "job_link_blocks": [b.model_dump(mode="json") for b in v.job_link_blocks],
            }  # fmt: skip
            found |= {f["code"] for f in checks.run(draft, ctx) if f["severity"] == checks.BLOCK}
    tags = set(case.get("tags") or [])
    if "golden" in tags and found:
        return False, f"golden copy blocked: {sorted(found)}"
    missing = set(expect.get("blocks") or []) - found
    if missing:
        return False, f"expected blocking findings missing: {sorted(missing)}"
    return True, "ok"


def run(root: Path = ROOT) -> dict[str, Any]:
    cases = load(root)
    results = {c["id"]: evaluate(c) for c in cases}
    redteam = [c["id"] for c in cases if "redteam" in (c.get("tags") or [])]
    passed = sum(1 for ok, _ in results.values() if ok)
    return {
        "cases": len(cases),
        "score": passed / len(cases) if cases else 0.0,
        "redteam": len(redteam),
        "redteam_rate": (sum(1 for i in redteam if results[i][0]) / len(redteam)) if redteam else 1.0,
        "failures": {k: why for k, (ok, why) in results.items() if not ok},
    }


__all__ = ["GATE", "ROOT", "evaluate", "load", "run"]
