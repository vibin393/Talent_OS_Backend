# ruff: noqa: E501
"""Epic 48 story 48.1 (A3, FR-244, NFR-37): CI guard -- no protected attribute, proxy, sub-city location, self-ID or
free-text field in any feature spec; extractors never import the fairness models (AD-47)."""

import ast
import dataclasses
from pathlib import Path

import pytest

from app.services.analytics.features import denylist
from app.services.analytics.features.specs import SPECS, FeatureDef, FeatureSpec

FEATURES_DIR = Path(__file__).resolve().parents[3] / "app" / "services" / "analytics" / "features"


@pytest.mark.parametrize("kind", sorted(SPECS))
def test_code_specs_are_clean(kind: str) -> None:
    assert denylist.violations(SPECS[kind].as_json()) == []


@pytest.mark.parametrize("kind", sorted(SPECS))
def test_every_feature_has_a_label(kind: str) -> None:
    spec = SPECS[kind]
    assert len(set(spec.keys)) == len(spec.keys)
    assert all(f.label.strip() for f in spec.features)


@pytest.mark.parametrize(
    ("key", "source"),
    [
        ("gender", "candidate.gender"),
        ("area", "candidate.pincode"),
        ("dob", "candidate.date_of_birth"),
        ("religion", "candidate.religion"),
        ("first_name", "candidate.first_name"),
        ("self_id", "demographic_self_id.answer"),
        ("notes", "application.notes"),
        ("jd", "demand.jd_text"),
        ("lat", "candidate.latitude"),
    ],
)
def test_adding_a_denylisted_feature_fails(key: str, source: str) -> None:
    spec = SPECS["offer_drop"]
    bad = dataclasses.replace(
        spec, features=(*spec.features, FeatureDef(key, key.title(), "numeric", source))
    )
    assert denylist.violations(bad.as_json())
    with pytest.raises(denylist.DenylistViolation):
        denylist.check_spec(bad.as_json())


def test_spec_type_is_frozen() -> None:
    assert isinstance(SPECS["attrition"], FeatureSpec)


def test_extractors_do_not_import_fairness() -> None:
    for path in FEATURES_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            for name in names:
                assert not name.startswith(("app.models.fairness", "app.services.fairness")), (
                    f"{path.name} imports {name}"
                )


def test_explanation_text_guard() -> None:
    assert denylist.text_violations("Higher risk mostly because of her age and religion") == [
        "age",
        "religion",
    ]
    assert (
        denylist.text_violations("A long notice period raises the risk; this is a supporting signal.") == []
    )
