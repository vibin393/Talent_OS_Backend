# ruff: noqa: E501
"""Exact decimal-string serialisation of model parameters (Epic 48 A4: no floats persisted, reproducible bytes).

``dec(x)`` is ``repr(float(x))`` -- the shortest string that round-trips the IEEE-754 double exactly, so
``float(dec(x)) == x`` and the same fit always gives the same bytes (``dumps`` sorts keys).
"""

import json
import math
from collections.abc import Mapping
from typing import Any


def dec(value: float) -> str:
    f = float(value)
    if math.isnan(f) or math.isinf(f):
        msg = "non-finite model parameter"
        raise ValueError(msg)
    return repr(f + 0.0)  # + 0.0 normalises -0.0


def num(value: str | float) -> float:
    return float(value)


def dumps(params: Mapping[str, Any]) -> bytes:
    return json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
