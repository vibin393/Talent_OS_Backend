"""47.2: no raw SQL and no expression input anywhere in the report builder."""

import re
from pathlib import Path

from pydantic import BaseModel

from app.schemas import report_builder as schemas

BUILDER = Path(__file__).resolve().parents[3] / "app" / "services" / "reporting" / "builder"
ROUTER = Path(__file__).resolve().parents[3] / "app" / "router" / "report_builder.py"
PATTERNS = [
    re.compile(r"\btext\("),
    re.compile(r"\bliteral_column\("),
    re.compile(r"\.execute\(\s*[\"']"),
    re.compile(r"\bexec_driver_sql\("),
    re.compile(r"f[\"'][^\"']*\b(SELECT|INSERT|UPDATE|DELETE|WHERE|FROM)\b", re.IGNORECASE),
    re.compile(r"[\"']\s*(SELECT|INSERT INTO|UPDATE|DELETE FROM)\s", re.IGNORECASE),
]
EXPRESSION_WORDS = ("expression", "formula", "sql", "raw", "code", "script")


def test_builder_has_no_raw_sql() -> None:
    files = [*BUILDER.glob("*.py"), ROUTER]
    assert len(files) >= 10
    hits = []
    for path in files:
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            hits.extend(f"{path.name}:{no}: {stripped}" for p in PATTERNS if p.search(line))
    assert hits == []


def test_no_input_model_accepts_an_expression() -> None:
    for name in dir(schemas):
        model = getattr(schemas, name)
        if not (isinstance(model, type) and issubclass(model, BaseModel)) or model is BaseModel:
            continue
        if not name.endswith(("In", "Create", "Update", "Spec")):
            continue  # responses
        assert model.model_config.get("extra") == "forbid", name
        for field in model.model_fields:
            assert not set(field.lower().split("_")) & set(EXPRESSION_WORDS), (name, field)
