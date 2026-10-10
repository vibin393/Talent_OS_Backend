# ruff: noqa: E501
"""Eval case model (AD-69). A case = seed + trigger + Fake LLM script (offline) + expectations (+ rubric for live mode).

Script values may use placeholders filled by the seed builder: ``{{candidate_id}}``, ``{{other_candidate_id}}``,
``{{owner_id}}``, ``{{random_id}}``.
"""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "stopped", "failed", "cancelled", "waiting"] | None = None
    reason_code: str | None = None
    tools_called: list[str] = Field(default_factory=list)
    tools_not_executed: list[str] = Field(default_factory=list)
    outcomes: dict[str, str] = Field(default_factory=dict)  # tool key -> outcome of its LAST call
    max_mode_by_tool: dict[str, Literal["off", "suggest", "auto"]] = Field(default_factory=dict)
    mode_reasons: dict[str, str] = Field(default_factory=dict)
    proposals: list[str] = Field(default_factory=list)  # tool keys of the proposals created
    no_auto_egress: bool = True
    no_cross_subject: bool = True
    fallback_task: bool | None = None
    activity_excludes: list[str] = Field(default_factory=list)
    max_tokens: int | None = None


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    agent: str = "_test_agent"
    seed: str = "candidate"
    subject: Literal["candidate", "none", "seed"] = "candidate"  # seed: Epic 56 (the builder names it)
    kickoff_untrusted: dict[str, str] = Field(default_factory=dict)
    script: list[dict[str, Any]]
    expect: Expect
    rubric: str | None = None  # live mode (Epic 65)
    tags: list[str] = Field(default_factory=list)


def load_cases(root: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for path in sorted(root.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        for item in raw if isinstance(raw, list) else [raw]:
            cases.append(EvalCase.model_validate(item))
    return cases


__all__ = ["EvalCase", "Expect", "load_cases"]
