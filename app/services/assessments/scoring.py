# ruff: noqa: E501
"""Pure answer scoring (Epic 40 A7, FR-205, NFR-36). No I/O, Decimal points only, no ``app`` imports.

**Candidate code is never executed.** Coding items are scored by comparison only: ``code_output`` compares the
predicted output (normalised, or a vetted ``re.fullmatch`` on a capped string), ``code_fill`` compares each blank
after token normalisation, ``code_order`` compares the line order (exact or LCS partial credit) and
``code_find_bug`` compares selected line numbers. ``code_freeform`` / ``essay`` always go to human review.

``score_answer`` never raises: an unexpected response shape scores 0 and is sent to review. Scores always lie in
``[0, points]`` and are deterministic. Responses carry the ORIGINAL option / line ids (the caller maps the opaque
per-attempt ids back before scoring).
"""

import dataclasses
import re
import string
import unicodedata
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")
MILLI = Decimal("0.001")
MAX_REGEX_INPUT = 2000
REVIEW_KINDS = frozenset({"code_freeform", "essay"})


@dataclasses.dataclass(frozen=True, slots=True)
class ScoreResult:
    points: Decimal
    confidence: Decimal
    needs_review: bool


def _dec(value: Any, default: Decimal = ZERO) -> Decimal:
    if isinstance(value, bool) or value is None:
        return default
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return default
    return d if d.is_finite() else default


def _q(value: Decimal, unit: Decimal = CENT) -> Decimal:
    return value.quantize(unit, rounding=ROUND_HALF_UP)


def _scaled(points: Decimal, fraction: Decimal) -> Decimal:
    fraction = min(max(fraction, ZERO), ONE)
    return min(max(_q(points * fraction), ZERO), points)


def _result(
    points: Decimal, fraction: Decimal, confidence: Decimal = ONE, review: bool = False
) -> ScoreResult:
    return ScoreResult(_scaled(points, fraction), _q(min(max(confidence, ZERO), ONE), MILLI), review)


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [v for v in value if isinstance(v, str)]


# ---------------------------------------------------------------- normalisation
_PUNCT = str.maketrans("", "", string.punctuation)


def normalise_text(text: str, flags: Mapping[str, Any] | None) -> str:
    flags = flags or {}
    out = unicodedata.normalize("NFKC", text)
    if flags.get("punctuation", True):
        out = out.translate(_PUNCT)
    if flags.get("case", True):
        out = out.casefold()
    if flags.get("whitespace", True):
        out = " ".join(out.split())
    return out.strip()


def normalise_output(text: str, flags: Mapping[str, Any] | None) -> str:
    flags = flags or {}
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if flags.get("trailing_ws", True):
        lines = [line.rstrip() for line in lines]
    if flags.get("blank_lines", True):
        lines = [line for line in lines if line.strip()]
    out = "\n".join(lines)
    if flags.get("trailing_ws", True):
        out = out.strip("\n")
    if flags.get("case", False):
        out = out.casefold()
    return out


_TOKEN_RE = re.compile(r"\w+|[^\w\s]")


def normalise_tokens(text: str, flags: Mapping[str, Any] | None = None) -> str:
    flags = flags or {}
    if flags.get("token_ws", True):
        return " ".join(_TOKEN_RE.findall(text))
    return text.strip()


def lcs_length(a: Sequence[str], b: Sequence[str]) -> int:
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b, start=1):
            cur.append(prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1]


# ---------------------------------------------------------------- per kind
def _choice(points: Decimal, key: Mapping[str, Any], response: Mapping[str, Any]) -> ScoreResult:
    correct = key.get("correct")
    return _result(points, ONE if isinstance(correct, str) and response.get("choice") == correct else ZERO)


def _multi(
    points: Decimal, key: Mapping[str, Any], response: Mapping[str, Any], partial: bool
) -> ScoreResult:
    correct = set(_str_list(key.get("correct")))
    chosen = set(_str_list(response.get("choices")))
    if not correct:
        return _result(points, ZERO)
    if chosen == correct:
        return _result(points, ONE)
    if not partial:
        return _result(points, ZERO)
    right = len(chosen & correct)
    wrong = len(chosen - correct)
    return _result(points, max(ZERO, Decimal(right - wrong) / Decimal(len(correct))))


def _numeric(points: Decimal, key: Mapping[str, Any], response: Mapping[str, Any]) -> ScoreResult:
    raw = response.get("value")
    if not isinstance(raw, str | int) or isinstance(raw, bool):
        return _result(points, ZERO)
    marker = object()
    value = _dec(raw, default=marker)  # type: ignore[arg-type]
    expected = _dec(key.get("value"), default=marker)  # type: ignore[arg-type]
    if value is marker or expected is marker:
        return _result(points, ZERO)
    diff = abs(value - expected)
    tol_abs = _dec(key.get("tolerance_abs"))
    tol_pct = _dec(key.get("tolerance_pct"))
    ok = diff <= tol_abs or (tol_pct > 0 and diff <= abs(expected) * tol_pct / Decimal(100))
    return _result(points, ONE if ok else ZERO)


def _short_text(
    points: Decimal, key: Mapping[str, Any], response: Mapping[str, Any], threshold: Decimal
) -> ScoreResult:
    text = _str(response.get("text"))[: MAX_REGEX_INPUT * 5]
    flags = key.get("normalise") if isinstance(key.get("normalise"), Mapping) else {}
    given = normalise_text(text, flags)
    if not given:
        return _result(points, ZERO)
    accepted = {normalise_text(a, flags) for a in _str_list(key.get("accepted"))}
    if given in accepted:
        return _result(points, ONE)
    keywords = key.get("keywords") if isinstance(key.get("keywords"), list) else []
    if not keywords:
        return _result(points, ZERO, ZERO, threshold > ZERO)
    words = set(given.split())
    score = ZERO
    for kw in keywords:
        if not isinstance(kw, Mapping):
            continue
        term = normalise_text(_str(kw.get("term")), flags)
        if not term:
            continue
        hit = (term in words) if " " not in term else (f" {term} " in f" {given} ")
        if hit:
            score += _dec(kw.get("weight"))
    score = min(max(score, ZERO), ONE)
    minimum = _dec(key.get("min_keyword_score"))
    fraction = score if score >= minimum else ZERO
    return _result(points, fraction, score, score < threshold)


def _code_output(points: Decimal, key: Mapping[str, Any], response: Mapping[str, Any]) -> ScoreResult:
    text = _str(response.get("text"))
    flags = key.get("normalise") if isinstance(key.get("normalise"), Mapping) else {}
    given = normalise_output(text, flags)
    if given and given in {normalise_output(a, flags) for a in _str_list(key.get("accepted"))}:
        return _result(points, ONE)
    pattern = key.get("regex")
    if isinstance(pattern, str) and pattern and len(text) <= MAX_REGEX_INPUT:
        try:
            if re.fullmatch(pattern, given) is not None:
                return _result(points, ONE)
        except re.error:
            return _result(points, ZERO)
    return _result(points, ZERO)


def _code_fill(points: Decimal, key: Mapping[str, Any], response: Mapping[str, Any]) -> ScoreResult:
    blanks = key.get("blanks")
    if not isinstance(blanks, Mapping) or not blanks:
        return _result(points, ZERO)
    given = response.get("blanks") if isinstance(response.get("blanks"), Mapping) else {}
    right = 0
    for n, spec in blanks.items():
        if not isinstance(spec, Mapping):
            continue
        flags = spec.get("normalise") if isinstance(spec.get("normalise"), Mapping) else {}
        answer = normalise_tokens(_str(given.get(str(n)))[:MAX_REGEX_INPUT], flags)
        if answer and answer in {normalise_tokens(a, flags) for a in _str_list(spec.get("accepted"))}:
            right += 1
    return _result(points, Decimal(right) / Decimal(len(blanks)))


def _code_order(points: Decimal, key: Mapping[str, Any], response: Mapping[str, Any]) -> ScoreResult:
    expected = _str_list(key.get("order"))
    given = _str_list(response.get("order"))[:500]
    if not expected:
        return _result(points, ZERO)
    if given == expected:
        return _result(points, ONE)
    if key.get("partial") == "lcs":
        return _result(points, Decimal(lcs_length(expected, given)) / Decimal(len(expected)))
    return _result(points, ZERO)


def _find_bug(points: Decimal, key: Mapping[str, Any], response: Mapping[str, Any]) -> ScoreResult:
    raw_bug = key.get("bug_lines") if isinstance(key.get("bug_lines"), list) else []
    bug = {v for v in raw_bug if isinstance(v, int) and not isinstance(v, bool)}
    raw_sel = response.get("lines") if isinstance(response.get("lines"), list) else []
    selected = {v for v in raw_sel[:1000] if isinstance(v, int) and not isinstance(v, bool)}
    if not bug:
        return _result(points, ZERO)
    penalty = len(selected - bug) if key.get("penalise_wrong", True) else 0
    return _result(points, max(ZERO, Decimal(len(selected & bug) - penalty)) / Decimal(len(bug)))


def score_answer(
    kind: str,
    item: Mapping[str, Any],
    response: Mapping[str, Any] | None,
    *,
    partial_credit: bool = False,
    confidence_threshold: Decimal = Decimal("0.8"),
) -> ScoreResult:
    """Score one answer. ``item`` = ``{points, answer_key, rubric}`` snapshot of the item version."""
    points = _dec(item.get("points") if isinstance(item, Mapping) else None, ONE)
    points = points if points > 0 else ONE
    if kind in REVIEW_KINDS:
        return ScoreResult(ZERO, ZERO, True)
    try:
        key = item.get("answer_key") if isinstance(item.get("answer_key"), Mapping) else {}
        resp = response if isinstance(response, Mapping) else {}
        if kind in ("single", "true_false"):
            return _choice(points, key, resp)
        if kind == "multi":
            return _multi(points, key, resp, partial_credit)
        if kind == "numeric":
            return _numeric(points, key, resp)
        if kind == "short_text":
            return _short_text(points, key, resp, confidence_threshold)
        if kind == "code_output":
            return _code_output(points, key, resp)
        if kind == "code_fill":
            return _code_fill(points, key, resp)
        if kind == "code_order":
            return _code_order(points, key, resp)
        if kind == "code_find_bug":
            return _find_bug(points, key, resp)
    except Exception:
        return ScoreResult(ZERO, ZERO, True)
    return ScoreResult(ZERO, ZERO, True)


def proficiency_for(pct: Decimal) -> int:
    """Per-skill percentage -> proficiency 1..5 (spec A7 [ASSUMPTION], coordinator decision 16)."""
    if pct >= 90:
        return 5
    if pct >= 75:
        return 4
    if pct >= 60:
        return 3
    if pct >= 40:
        return 2
    return 1


def pct(points: Decimal, max_points: Decimal) -> Decimal:
    if max_points <= 0:
        return ZERO
    return min(max(_q(points / max_points * Decimal(100)), ZERO), Decimal(100))


__all__ = [
    "REVIEW_KINDS",
    "ScoreResult",
    "lcs_length",
    "normalise_output",
    "normalise_text",
    "normalise_tokens",
    "pct",
    "proficiency_for",
    "score_answer",
]
