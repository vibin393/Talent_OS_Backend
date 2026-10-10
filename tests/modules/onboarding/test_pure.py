"""Pure rules: template matching, status machines, sensitive-data guards, portal field visibility."""

import ast
import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.errors import ConflictError, ValidationFailed
from app.models.onboarding import OnboardingAcknowledgement, OnboardingCase, OnboardingTask
from app.schemas.onboarding_portal import PortalCaseOut, PortalTaskOut
from app.services.onboarding import matching, status
from app.services.onboarding.common import assert_no_full_aadhaar, assert_no_sensitive_keys, scrub_draft

CLIENT, LE = uuid.uuid4(), uuid.uuid4()
FACTS = matching.PlacementFacts(client_id=CLIENT, legal_entity_id=LE, worker_type="employee", country="IN")
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@dataclasses.dataclass
class Tpl:
    name: str
    client_id: uuid.UUID | None = None
    legal_entity_id: uuid.UUID | None = None
    worker_type: str | None = None
    country: str | None = None
    created_at: datetime = T0
    id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)


@pytest.mark.parametrize(
    ("templates", "expected"),
    [
        ([Tpl("any"), Tpl("country", country="IN")], "country"),
        ([Tpl("le", legal_entity_id=LE), Tpl("wt", worker_type="employee")], "le"),
        (
            [Tpl("client", client_id=CLIENT), Tpl("le+wt", legal_entity_id=LE, worker_type="employee")],
            "le+wt",
        ),
        ([Tpl("client", client_id=CLIENT), Tpl("le", legal_entity_id=LE)], "client"),
        ([Tpl("other", client_id=uuid.uuid4()), Tpl("any")], "any"),
        ([Tpl("wrong", worker_type="contractor")], None),
        ([Tpl("old", country="IN"), Tpl("new", country="in", created_at=T0 + timedelta(days=1))], "new"),
    ],
)
def test_matching_specificity(templates: list[Tpl], expected: str | None) -> None:
    picked = matching.select(templates, FACTS)
    assert (picked.name if picked else None) == expected


@settings(max_examples=60, deadline=None)
@given(st.permutations(list(range(6))))
def test_matching_is_order_independent(order: list[int]) -> None:
    pool = [
        Tpl("any"),
        Tpl("c", client_id=CLIENT),
        Tpl("le", legal_entity_id=LE),
        Tpl("c2", client_id=CLIENT, created_at=T0 + timedelta(days=2)),
        Tpl("wt", worker_type="employee"),
        Tpl("x", client_id=uuid.uuid4()),
    ]
    baseline = matching.select(pool, FACTS)
    assert baseline is not None and baseline.name == "c2"
    assert matching.select([pool[i] for i in order], FACTS) is baseline


@pytest.mark.parametrize(
    ("src", "dst", "actor", "ok"),
    [
        ("pending", "submitted", "candidate", True),
        ("returned", "submitted", "candidate", True),
        ("submitted", "approved", "user", True),
        ("submitted", "approved", "system", True),
        ("submitted", "approved", "ai", False),
        ("submitted", "returned", "user", True),
        ("pending", "approved", "system", True),
        ("pending", "approved", "user", False),
        ("approved", "returned", "user", False),
        ("pending", "waived", "user", True),
        ("submitted", "waived", "user", True),
        ("pending", "waived", "workflow", False),
        ("pending", "waived", "system", False),
        ("pending", "waived", "ai", False),
    ],
)
def test_task_transitions(src: str, dst: str, actor: str, ok: bool) -> None:
    if ok:
        status.check_task(src, dst, actor)
    else:
        with pytest.raises(ConflictError) as exc:
            status.check_task(src, dst, actor)
        assert exc.value.code == "transition_not_allowed"


def test_complete_case_is_never_reopened() -> None:
    for to in ("in_progress", "not_started"):
        with pytest.raises(ConflictError):
            status.check_case("complete", to, "system")
    status.check_case("in_progress", "complete", "system")
    with pytest.raises(ConflictError):
        status.check_case("in_progress", "complete", "ai")


def test_no_sensitive_columns_on_onboarding_tables() -> None:
    import re

    bad = re.compile(r"account_number|^pan$|pan_number|aadhaar|passport")
    for model in (OnboardingCase, OnboardingTask, OnboardingAcknowledgement):
        cols = [c.name for c in model.__table__.columns]
        assert not [c for c in cols if bad.search(c)], model.__tablename__


def test_sensitive_key_guards() -> None:
    with pytest.raises(ValueError):
        assert_no_sensitive_keys({"account_number": "1"})
    with pytest.raises(ValueError):
        assert_no_sensitive_keys({"nested": {"pan": "ABCPE1234F"}})
    assert_no_sensitive_keys({"last4": "9012", "pan_last4": "234F"})
    assert scrub_draft({"account_number": "1", "pan": "X", "holder_name": "A"}) == {"holder_name": "A"}


def test_full_aadhaar_rejected_anywhere_but_bank_numbers_pass() -> None:
    for payload in (
        {"note": "my aadhaar 1234 5678 9012"},
        {"x": ["123456789012"]},
        {"a": {"b": "1234-5678-9012"}},
    ):
        with pytest.raises(ValidationFailed) as exc:
            assert_no_full_aadhaar(payload)
        assert exc.value.code == "full_aadhaar_not_allowed"
    assert_no_full_aadhaar({"account_number": "123456789012", "account_number_confirm": "123456789012"})
    assert_no_full_aadhaar({"aadhaar_last4": "9012", "phone": "+91 98765 43210"})


def test_portal_schemas_omit_internal_fields() -> None:
    fields = set(PortalTaskOut.model_fields) | set(PortalCaseOut.model_fields)
    for forbidden in (
        "owner_user_id",
        "decided_by_id",
        "decided_by_kind",
        "template_snapshot",
        "source",
        "p1_task_id",
        "assignee",
        "data",
    ):
        assert forbidden not in fields, forbidden


def test_onboarding_never_imports_siblings() -> None:
    root = Path(__file__).resolve().parents[3] / "app" / "services" / "onboarding"
    forbidden = ("app.services.compliance", "app.services.bgv", "app.services.ai")
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for n in names:
                assert not n.startswith(forbidden), f"{path.name}: {n}"
