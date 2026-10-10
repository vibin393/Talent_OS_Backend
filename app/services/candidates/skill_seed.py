"""Platform skill catalog seed (``data/skills_seed.yaml``): idempotent upsert by slug.

Runs as the owner role (``talent_app`` only has SELECT on ``skills`` / ``skill_aliases``): from migration
20261001_32 (``sync_seed(op.get_bind())``) and ``python -m app.cli seed-skills``.
"""

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped,unused-ignore]
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.services.candidates.common import normalize_skill, slugify

SEED_FILE = Path(__file__).resolve().parent / "data" / "skills_seed.yaml"


@dataclass(frozen=True)
class SeedSkill:
    name: str
    slug: str
    category: str | None
    parent: str | None
    aliases: tuple[str, ...] = field(default_factory=tuple)


def load_seed(path: Path = SEED_FILE) -> list[SeedSkill]:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: list[SeedSkill] = []
    for entry in raw["skills"]:
        name = str(entry["name"]).strip()
        aliases = {normalize_skill(str(a)) for a in entry.get("aliases") or ()}
        aliases.discard("")
        aliases.discard(normalize_skill(name))
        out.append(
            SeedSkill(
                name=name,
                slug=slugify(name),
                category=entry.get("category"),
                parent=str(entry["parent"]).strip() if entry.get("parent") else None,
                aliases=tuple(sorted(aliases)),
            )
        )
    return out


def sync_seed(conn: Connection, path: Path = SEED_FILE) -> dict[str, int]:
    """Upsert every seed skill, its parent link and aliases. Returns counts. Safe to re-run."""
    skills = load_seed(path)
    by_name = {s.name: s for s in skills}
    for s in skills:
        conn.execute(
            text(
                "INSERT INTO skills (id, name, slug, category, is_active) "
                "VALUES (:id, :name, :slug, :category, true) "
                "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, category = EXCLUDED.category, "
                "is_active = true, updated_at = now()"
            ),
            {"id": uuid.uuid4(), "name": s.name, "slug": s.slug, "category": s.category},
        )
    ids = {row[0]: row[1] for row in conn.execute(text("SELECT slug, id FROM skills"))}
    for s in skills:
        parent = by_name.get(s.parent) if s.parent else None
        conn.execute(
            text(
                "UPDATE skills SET parent_id = :parent "
                "WHERE slug = :slug AND parent_id IS DISTINCT FROM :parent"
            ),
            {"parent": ids[parent.slug] if parent else None, "slug": s.slug},
        )
    alias_count = 0
    for s in skills:
        for alias in s.aliases:
            conn.execute(
                text(
                    "INSERT INTO skill_aliases (id, skill_id, alias_norm) VALUES (:id, :skill, :alias) "
                    "ON CONFLICT (alias_norm) DO UPDATE SET skill_id = EXCLUDED.skill_id, updated_at = now()"
                ),
                {"id": uuid.uuid4(), "skill": ids[s.slug], "alias": alias},
            )
            alias_count += 1
    return {"skills": len(skills), "aliases": alias_count}
