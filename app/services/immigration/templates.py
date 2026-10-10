# ruff: noqa: E501
"""Platform visa type templates (Epic 46 A1): ``data/visa_type_templates.yaml`` -> ``visa_type_templates``.

``sync_seed(conn)`` is an idempotent upsert by ``key`` that bumps ``template_version`` when a template's content
changes. It runs as the owner role (``talent_app`` only has SELECT): from migration ``20261007_310_01`` and
``python -m app.cli seed-visa-templates``. Templates are platform data; adopting copies them into a tenant
``visa_types`` row, and tenant edits never change a template.
"""

import json
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped,unused-ignore]
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.services.immigration import definition as defs

SEED_FILE = Path(__file__).resolve().parent / "data" / "visa_type_templates.yaml"


def load_templates(path: Path = SEED_FILE) -> list[dict[str, Any]]:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: list[dict[str, Any]] = []
    for t in raw["templates"]:
        d = defs.validate(t["definition"])
        reviewed = t["last_reviewed"]
        out.append(
            {
                "key": str(t["key"]),
                "country": str(t["country"]).upper(),
                "name": str(t["name"]),
                "category": str(t.get("category") or "work"),
                "definition": defs.dump(d),
                "last_reviewed": reviewed
                if isinstance(reviewed, date)
                else date.fromisoformat(str(reviewed)),
                "notice": str(t.get("notice") or raw.get("notice") or ""),
            }
        )
    return out


def _content(t: dict[str, Any]) -> str:
    return json.dumps(
        {k: t[k] for k in ("country", "name", "category", "definition", "notice")},
        sort_keys=True,
        default=str,
    )


def sync_seed(conn: Connection, path: Path = SEED_FILE) -> dict[str, int]:
    """Upsert every template by key. Returns ``{"inserted": n, "updated": n, "unchanged": n}``."""
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for t in load_templates(path):
        row = conn.execute(
            text(
                "SELECT country, name, category, definition, notice, template_version FROM visa_type_templates "
                "WHERE key = :key"
            ),
            {"key": t["key"]},
        ).first()
        params = {**t, "definition": json.dumps(t["definition"])}
        if row is None:
            conn.execute(
                text(
                    "INSERT INTO visa_type_templates (id, key, country, name, category, definition, template_version, "
                    "last_reviewed, notice) VALUES (:id, :key, :country, :name, :category, CAST(:definition AS jsonb), "
                    "1, :last_reviewed, :notice)"
                ),
                {"id": uuid.uuid4(), **params},
            )
            counts["inserted"] += 1
            continue
        current = {
            "country": row.country,
            "name": row.name,
            "category": row.category,
            "definition": row.definition,
            "notice": row.notice,
        }
        if _content(current) == _content(t):
            conn.execute(
                text(
                    "UPDATE visa_type_templates SET last_reviewed = :last_reviewed WHERE key = :key "
                    "AND last_reviewed IS DISTINCT FROM :last_reviewed"
                ),
                {"key": t["key"], "last_reviewed": t["last_reviewed"]},
            )
            counts["unchanged"] += 1
            continue
        conn.execute(
            text(
                "UPDATE visa_type_templates SET country = :country, name = :name, category = :category, "
                "definition = CAST(:definition AS jsonb), notice = :notice, last_reviewed = :last_reviewed, "
                "template_version = template_version + 1, updated_at = now() WHERE key = :key"
            ),
            params,
        )
        counts["updated"] += 1
    return counts
