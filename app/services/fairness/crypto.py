# ruff: file-ignore[line-too-long]
"""Sealing of self-ID answers (Epic 49 A2).

A thin wrapper over the foundation ``EncryptedJSON`` helpers with the dedicated ``demographic`` key purpose. ``Sealed`` never prints or serialises."""

import uuid
from collections.abc import Mapping
from typing import Any

from app.core.encrypted import Sealed, open_json, seal_json

PURPOSE = "demographic"


def seal_answers(tenant_id: uuid.UUID, answers: Mapping[str, str]) -> Sealed:
    return seal_json(tenant_id, PURPOSE, {str(k): str(v) for k, v in sorted(answers.items())})


def open_answers(tenant_id: uuid.UUID, sealed: Sealed) -> dict[str, Any]:
    value = open_json(tenant_id, PURPOSE, sealed)
    return dict(value) if isinstance(value, dict) else {}
