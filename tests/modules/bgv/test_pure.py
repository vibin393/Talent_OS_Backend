"""Pure Epic 20 rules: status matrix (★ rows), BgvCheckJob minimisation, no-AI import graph, phone / Aadhaar
validation, portal schemas never carrying reference answers."""

import ast
import dataclasses
import uuid
from pathlib import Path

import pytest

from app.core.errors import ConflictError, ValidationFailed
from app.models.bgv import BgvCheck, BgvDiscrepancy
from app.schemas import bgv_portal
from app.services.bgv import status as st
from app.services.bgv.common import add_working_days
from app.services.bgv.portal import _aadhaar_last4
from app.services.connectors.bgv.port import BgvCheckJob
from tests.modules.bgv.helpers import full_aadhaar


@pytest.mark.parametrize("to", ["clear", "failed"])
@pytest.mark.parametrize("kind", ["ai", "workflow", "system", "api_key", "candidate"])
def test_adjudication_rows_are_human_only(to: str, kind: str) -> None:
    with pytest.raises(ConflictError) as exc:
        st.check_transition("under_review", to, kind)
    assert exc.value.code == "transition_not_allowed"
    st.check_transition("under_review", to, "user")


def test_matrix_automation_is_system_only_and_clear_only_from_under_review() -> None:
    for (a, b), kinds in st.TRANSITIONS.items():
        if b in ("in_verification", "discrepancy", "under_review", "documents_pending"):
            assert kinds == frozenset({"system"}), (a, b)
    with pytest.raises(ConflictError):
        st.check_transition("discrepancy", "clear", "user")
    st.check_transition("discrepancy", "failed", "user")
    with pytest.raises(ConflictError):
        st.check_transition("in_verification", "under_review", "user")


def _check(status: str) -> BgvCheck:
    return BgvCheck(id=uuid.uuid4(), status=status)


def _disc(check: BgvCheck, status: str) -> BgvDiscrepancy:
    return BgvDiscrepancy(id=uuid.uuid4(), check_id=check.id, status=status)


def test_recompute_targets() -> None:
    a, b = _check("discrepancy"), _check("in_progress")
    assert st.target_status([a, b], [_disc(a, "awaiting_response")]) == "discrepancy"
    assert st.target_status([a, b], [_disc(a, "resolved")]) == "in_verification"
    b.status = "verified"
    assert st.target_status([a, b], [_disc(a, "awaiting_response")]) == "discrepancy"
    assert st.target_status([a, b], [_disc(a, "responded")]) == "under_review"
    assert st.target_status([a, b], [_disc(a, "lapsed")]) == "under_review"


def test_check_job_carries_only_minimal_fields() -> None:
    assert [f.name for f in dataclasses.fields(BgvCheckJob)] == [
        "check_id",
        "check_type",
        "subject_label",
        "file_ids",
        "declarations",
    ]
    payload = BgvCheckJob(uuid.uuid4(), "employment", "Employer 1: Infosys").to_payload()
    assert set(payload) == {"check_id", "check_type", "subject_label", "file_ids", "declarations"}


def test_bgv_never_imports_ai_compliance_or_onboarding() -> None:
    root = Path(__file__).resolve().parents[3] / "app" / "services"
    files = [
        *(root / "bgv").rglob("*.py"),
        root / "bgv_erasure.py",
        *(root / "connectors" / "bgv").rglob("*.py"),
    ]
    banned = ("app.services.ai", "app.services.compliance", "app.services.onboarding")
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            mods: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            for mod in mods:
                assert not any(
                    mod == b or mod.startswith(b + ".") or mod.startswith(b + "_") for b in banned
                ), (path, mod)


def test_aadhaar_and_phone_rules() -> None:
    with pytest.raises(ValidationFailed) as exc:
        _aadhaar_last4(full_aadhaar())
    assert exc.value.code == "aadhaar_full_number_forbidden"
    with pytest.raises(ValidationFailed) as exc:
        _aadhaar_last4("1234 5678 9012")
    assert exc.value.code == "aadhaar_full_number_forbidden"
    assert _aadhaar_last4("4321") == "4321"
    assert bgv_portal.normalise_phone("98450 12345") == "+919845012345"
    assert bgv_portal.normalise_phone("+44 20 7946 0958") == "+442079460958"
    with pytest.raises(ValueError):
        bgv_portal.normalise_phone("12345")


def test_working_days_skip_weekends() -> None:
    from datetime import date

    assert add_working_days(date(2026, 10, 2), 1) == date(2026, 10, 5)  # Friday -> Monday
    assert add_working_days(date(2026, 10, 5), 5) == date(2026, 10, 12)


def test_portal_schemas_never_carry_reference_answers_or_reasons() -> None:
    for name in dir(bgv_portal):
        model = getattr(bgv_portal, name)
        fields = getattr(model, "model_fields", None)
        if not fields:
            continue
        for forbidden in ("answers", "adjudication_reasons", "internal_note", "verifier_name", "result_note"):
            assert forbidden not in fields, (name, forbidden)
