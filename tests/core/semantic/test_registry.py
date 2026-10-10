"""Semantic registry: registration is validated at import / startup (Epic 36 story 36.2)."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from app.core.semantic import (
    FieldDef,
    JoinDef,
    ScopeSpec,
    SemanticRegistryError,
    extend_semantic_source,
    get_source,
    register_semantic_source,
    sources,
)
from app.core.semantic.registry import unregister_semantic_source
from app.models.candidates import Candidate, CandidateTag
from app.services.candidates import semantic_source as candidate_source
from app.services.placements import semantic_source as placement_source

KEY = "testreg.thing"
SCOPE = ScopeSpec(permission="candidate:read", owner_cols=(lambda c: c.owner_id,))


def _field(**kw: Any) -> FieldDef:
    base: dict[str, Any] = {
        "type": "string",
        "label": "Name",
        "pii_class": "none",
        "expr": lambda c: c.full_name,
        "ops": frozenset({"eq"}),
    }
    base.update(kw)
    return FieldDef(**base)


def _register(fields: dict[str, FieldDef], joins: dict[str, JoinDef] | None = None, **kw: Any) -> None:
    register_semantic_source(
        kw.pop("key", KEY),
        Candidate,
        fields,
        joins or {},
        kw.pop("scope", SCOPE),
        label="Test",
        subject=lambda c: c.id,
        **kw,
    )


@pytest.fixture(autouse=True)
def _cleanup() -> Iterator[None]:
    yield
    unregister_semantic_source(KEY)


def test_valid_registration_and_lookup() -> None:
    _register({"name": _field()})
    assert get_source(KEY).fields["name"].label == "Name"
    assert KEY in sources()


def test_protected_refused() -> None:
    with pytest.raises(SemanticRegistryError, match="protected"):
        _register({"gender": _field(pii_class="protected")})
    assert KEY not in sources()


@pytest.mark.parametrize("pii", [None, "", "secret"])
def test_missing_or_unknown_pii_refused(pii: Any) -> None:
    with pytest.raises(SemanticRegistryError, match="pii_class"):
        _register({"name": _field(pii_class=pii)})


@pytest.mark.parametrize("permission", ["", "nope:nothing"])
def test_missing_or_unknown_scope_permission_refused(permission: str) -> None:
    with pytest.raises(SemanticRegistryError, match="permission"):
        _register(
            {"name": _field()}, scope=ScopeSpec(permission=permission, owner_cols=(lambda c: c.owner_id,))
        )


def test_unknown_field_permission_refused() -> None:
    with pytest.raises(SemanticRegistryError, match="permission"):
        _register({"name": _field(permission="made:up")})


@pytest.mark.parametrize("key", ["candidates", "Candidates.x", "a.b.c", "a-b.c", "1a.b", ""])
def test_bad_source_keys(key: str) -> None:
    with pytest.raises(SemanticRegistryError, match="source key"):
        _register({"name": _field()}, key=key)


@pytest.mark.parametrize("fkey", ["Name", "a.b.c", "1a", "a-b", "a."])
def test_bad_field_keys(fkey: str) -> None:
    with pytest.raises(SemanticRegistryError):
        _register({fkey: _field()})


def test_unknown_join_refused() -> None:
    with pytest.raises(SemanticRegistryError, match="unknown join"):
        _register({"tags.name": _field(join="tags")})
    with pytest.raises(SemanticRegistryError, match="unknown join"):
        _register({"thing": _field(join="nowhere")})


def test_many_join_field_not_groupable() -> None:
    joins = {"tags": JoinDef(CandidateTag, lambda c, t: t.candidate_id == c.id, many=True)}
    with pytest.raises(SemanticRegistryError, match="filter-only"):
        _register(
            {"tags.tag_id": _field(type="uuid", expr=lambda t: t.tag_id, join="tags", groupable=True)}, joins
        )


def test_operator_type_mismatch_refused() -> None:
    with pytest.raises(SemanticRegistryError, match="do not fit"):
        _register({"name": _field(ops=frozenset({"gt"}))})
    with pytest.raises(SemanticRegistryError, match="enum_values"):
        _register({"name": _field(type="enum")})
    with pytest.raises(SemanticRegistryError, match="skill_predicate"):
        _register({"name": _field(type="skill", ops=frozenset({"skill_match"}))})


def test_duplicate_source_refused() -> None:
    _register({"name": _field()})
    with pytest.raises(SemanticRegistryError, match="duplicate"):
        _register({"name": _field()})


def test_extend_adds_fields_and_refuses_duplicates() -> None:
    _register({"name": _field()})
    extend_semantic_source(KEY, fields={"title": _field(expr=lambda c: c.current_title)})
    assert set(get_source(KEY).fields) == {"name", "title"}
    with pytest.raises(SemanticRegistryError, match="duplicate"):
        extend_semantic_source(KEY, fields={"title": _field()})
    with pytest.raises(SemanticRegistryError, match="protected"):
        extend_semantic_source(KEY, fields={"caste": _field(pii_class="protected")})


def test_extend_before_register_is_applied_on_registration() -> None:
    extend_semantic_source(KEY, fields={"title": _field(expr=lambda c: c.current_title)})
    _register({"name": _field()})
    assert set(get_source(KEY).fields) == {"name", "title"}


def test_base_sources_registered_with_contract_fields() -> None:
    candidate_source.register()
    placement_source.register()
    candidate_source.register()  # idempotent
    cand = get_source("candidates.candidate")
    base = {
        "id", "human_id", "full_name", "current_title", "current_employer", "skills", "experience_years",
        "city", "state", "country", "notice_period_days", "availability_date", "expected_comp_inr",
        "work_authorization", "willing_to_relocate", "tags.name", "source", "owner_id", "created_at",
        "last_updated_at", "is_archived", "stage_history.stage_key", "stage_history.entered_at",
        "placements.status", "placements.worker_type", "placements.end_date", "bench.status",
        "consent_marketing", "consent_engagement",
    }  # fmt: skip
    # Later epics extend the source via ``extend_semantic_source`` once their hooks load (segments,
    # campus Epic 43); only those deliberate extensions may appear on top of the base contract.
    extensions = {
        "days_since_last_placement", "campus_batch", "campus_registrations.drive_id",
        "campus_registrations.status", "campus_registrations.batch_year",
    }  # fmt: skip
    assert base <= set(cand.fields)
    assert set(cand.fields) - base <= extensions
    assert cand.fields["expected_comp_inr"].pii_class == "compensation"
    assert cand.fields["consent_marketing"].ops == frozenset()
    assert cand.joins["placements"].many and cand.joins["placements"].permission == "placement:read"
    assert cand.joins["bench"].permission == "bench:read"
    assert all(f.pii_class != "protected" for f in cand.fields.values())
    worker = get_source("placements.worker")
    assert {"candidate_id", "effective_end_date", "skills", "candidate.full_name", "candidate.city"} <= set(
        worker.fields
    )
    assert worker.scope.permission == "placement:read" and len(worker.scope.owner_cols) == 2


def test_unknown_source_lookup_raises_key_error() -> None:
    with pytest.raises(KeyError):
        get_source(f"nope.{uuid.uuid4().hex[:6]}")
