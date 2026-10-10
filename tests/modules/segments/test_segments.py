"""Epic 38 story 38.1: segments on the semantic layer (FR-192, NFR-33, AD-42)."""

import uuid
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.database import tenant_session
from app.core.errors import ValidationFailed
from app.core.semantic import Definition, SemanticError, register_skill_expander, validate
from app.core.semantic import operators as semantic_operators
from app.core.skill_refs import ExpandedSkill, SkillKey
from app.schemas.segments import SegmentCreate
from app.services import segments_service
from app.services.segments import crud, definitions
from app.services.segments import semantic_fields as fields
from tests.epic5 import h
from tests.modules.demands.helpers import DOrg
from tests.modules.talent_pools.helpers import SEGMENTS, candidate, ctx_for




async def _create(org: DOrg, definition: dict[str, Any], **body: Any) -> Any:
    return await org.admin.client.post(
        SEGMENTS,
        json={"name": "Seg", "source_key": "candidates.candidate", "definition": definition, **body},
        headers=h(org.admin),
    )


async def test_create_edit_versions_and_preview(dorg: DOrg) -> None:
    await candidate(dorg.admin, "Asha Quick", notice_period_days=10)
    await candidate(dorg.admin, "Ben Slow", notice_period_days=90)
    resp = await _create(dorg, {"filters": {"field": "notice_period_days", "op": "lte", "value": 30}})
    assert resp.status_code == 201, resp.text
    seg = resp.json()
    assert seg["definition_version"] == 1
    assert seg["definition"]["source"] == "candidates.candidate"

    patched = await dorg.admin.client.patch(
        f"{SEGMENTS}/{seg['id']}",
        json={"definition": {"filters": {"field": "notice_period_days", "op": "lte", "value": 60}}},
        headers=h(dorg.admin),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["definition_version"] == 2
    versions = (await dorg.admin.client.get(f"{SEGMENTS}/{seg['id']}/versions")).json()
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[1]["definition"]["filters"]["value"] == 30  # v1 stays readable

    renamed = await dorg.admin.client.patch(f"{SEGMENTS}/{seg['id']}", json={"name": "Renamed"}, headers=h(dorg.admin))
    assert renamed.json()["definition_version"] == 2  # no definition change, no new version

    preview = await dorg.admin.client.post(
        f"{SEGMENTS}/preview",
        json={"source_key": "candidates.candidate", "definition": {"filters": {"field": "notice_period_days", "op": "lte", "value": 30}}},
        headers=h(dorg.admin),
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["count"] == 1
    assert [r["full_name"] for r in body["rows"]] == ["Asha Quick"]


@pytest.mark.parametrize(
    ("definition", "code"),
    [
        ({"filters": {"field": "salary", "op": "eq", "value": 1}}, "semantic_unknown_field"),
        ({"filters": {"field": "notice_period_days", "op": "contains", "value": "x"}}, "semantic_operator_not_allowed"),
        ({"filters": {"field": "notice_period_days", "op": "gt", "value": "ten"}}, "semantic_value_type"),
        ({"filters": {"field": "consent_marketing", "op": "eq", "value": "granted"}}, "semantic_operator_not_allowed"),
        ({"filters": {"field": "ghost.x", "op": "eq", "value": 1}}, "semantic_unknown_join"),
    ],
)
async def test_invalid_definitions_are_422(dorg: DOrg, definition: dict[str, Any], code: str) -> None:
    resp = await _create(dorg, definition)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == code


async def test_fields_and_groups_refused(dorg: DOrg) -> None:
    resp = await _create(dorg, {"filters": {"field": "notice_period_days", "op": "lte", "value": 1}, "fields": ["id"]})
    assert resp.status_code == 422
    resp = await _create(dorg, {"filters": None})
    assert resp.status_code == 422


async def test_compensation_filter_needs_permission(dorg: DOrg) -> None:
    ctx = ctx_for(dorg.tenant_id, dorg.admin.user_id)
    perms = dict(ctx.principal.permissions)
    perms.pop("compensation:view", None)
    import dataclasses

    ctx = dataclasses.replace(ctx, principal=dataclasses.replace(ctx.principal, permissions=perms))
    body = SegmentCreate(
        name="Comp",
        source_key="candidates.candidate",
        definition={"filters": {"field": "expected_comp_inr", "op": "lt", "value": 1500000}},
    )
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(SemanticError) as exc:
            await crud.create_segment(ctx, db, body)
    assert exc.value.code == "semantic_field_forbidden"


async def test_definition_naming_a_candidate_is_refused(dorg: DOrg) -> None:
    cid = await candidate(dorg.admin, "Named Person")
    resp = await _create(dorg, {"filters": {"field": "id", "op": "eq", "value": str(cid)}})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "segment_person_reference"


async def test_tenant_visibility_needs_pool_manage(dorg: DOrg) -> None:
    from app.core.permissions import Scope

    ctx = ctx_for(dorg.tenant_id, dorg.admin.user_id)
    perms = dict(ctx.principal.permissions)
    perms.pop("pool:manage")
    import dataclasses

    ctx = dataclasses.replace(ctx, principal=dataclasses.replace(ctx.principal, permissions=perms))
    body = SegmentCreate(
        name="T",
        source_key="candidates.candidate",
        definition={"filters": {"field": "notice_period_days", "op": "lte", "value": 1}},
        visibility="tenant",
    )
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(Exception) as exc:
            await crud.create_segment(ctx, db, body)
    assert getattr(exc.value, "status_code", None) == 403
    assert Scope.TENANT  # keep import used


async def test_evaluate_contract(dorg: DOrg) -> None:
    await candidate(dorg.admin, "Asha", notice_period_days=5)
    ctx = ctx_for(dorg.tenant_id, dorg.admin.user_id)
    defn = validate({"source": "candidates.candidate", "filters": {"field": "notice_period_days", "op": "lte", "value": 5}})
    async with tenant_session(dorg.tenant_id) as db:
        with pytest.raises(ValidationFailed) as exc:
            await segments_service.evaluate(ctx, db)
        assert exc.value.code == "segment_or_definition"
        ids = await segments_service.evaluate(ctx, db, definition=defn, mode="ids", with_reasons=True)
        counted = await segments_service.evaluate(ctx, db, definition=defn, mode="count")
        preview = await segments_service.evaluate(ctx, db, definition=defn, mode="preview", limit=10)
    assert ids.count == counted.count == preview.count == 1
    assert counted.subject_ids == ()
    assert ids.reasons is not None
    reason = ids.reasons[ids.subject_ids[0]]
    assert reason["matched"][0]["field"] == "notice_period_days"


async def test_skill_match_expand_uses_registered_expander_and_records_skills(
    dorg: DOrg, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = SkillKey(kind="global", id=uuid.uuid4())
    related = SkillKey(kind="global", id=uuid.uuid4())
    calls: list[Any] = []
    previous = semantic_operators._expander

    async def spy(ctx: Any, db: Any, refs: Any, depth: int) -> list[ExpandedSkill]:
        calls.append((list(refs), depth))
        return [
            ExpandedSkill(key=key, root=key, weight=Decimal("1.00"), depth=0, via=None),
            ExpandedSkill(key=related, root=key, weight=Decimal("0.80"), depth=1, via=key),
        ]

    async def fake_expand(ctx: Any, db: Any, refs: Any, *, depth: int = 1, **_: Any) -> list[ExpandedSkill]:
        return await spy(ctx, db, refs, depth)

    from app.services import skills_service

    monkeypatch.setattr(skills_service, "expand", fake_expand)
    register_skill_expander(spy)
    try:
        defn = validate(
            {
                "source": "candidates.candidate",
                "filters": {
                    "field": "skills",
                    "op": "skill_match",
                    "value": {"ref": {"kind": "global", "id": str(key.id)}, "min_proficiency": 3, "expand": True},
                },
            }
        )
        ctx = ctx_for(dorg.tenant_id, dorg.admin.user_id)
        async with tenant_session(dorg.tenant_id) as db:
            await segments_service.evaluate(ctx, db, definition=defn, mode="count")
            assert calls and calls[-1][1] == 1  # compile went through the registered expander (depth 1)
            reason = await segments_service.evaluate(ctx, db, definition=defn, mode="ids", with_reasons=True)
        assert reason.reasons == {}  # nobody has these random skills
        from app.services.segments.service import reason_for

        async with tenant_session(dorg.tenant_id) as db:
            shared = await reason_for(ctx, db, defn, None, 1)
        assert {"skill_key": related.as_str(), "weight": "0.80"} in shared["skills"]
    finally:
        register_skill_expander(previous)


async def test_added_fields_are_registered() -> None:
    from app.core.semantic import get_source

    fields.register()
    assert "days_since_last_placement" in get_source("candidates.candidate").fields
    assert "offboarding.reason" in get_source("placements.worker").fields
    assert get_source("candidates.candidate").fields["days_since_last_placement"].permission == "placement:read"


# ---------------------------------------------------------------- fuzz (hypothesis): 422 or valid, never a crash
FIELD_NAMES = ["notice_period_days", "city", "skills", "salary", "consent_marketing", "ghost.x", "", "1=1", "tags.name"]
OPS = ["eq", "in", "lt", "gt", "contains", "within_days", "skill_match", "regex", "is_null", ""]
values = st.one_of(
    st.none(), st.booleans(), st.integers(-(2**33), 2**33), st.text(max_size=8),
    st.lists(st.integers(0, 5), max_size=3), st.dictionaries(st.sampled_from(["days", "ref", "expand"]), st.integers(0, 4), max_size=2),
)  # fmt: skip
leaves = st.fixed_dictionaries({"field": st.sampled_from(FIELD_NAMES), "op": st.sampled_from(OPS), "value": values})
trees = st.recursive(
    leaves,
    lambda inner: st.one_of(
        st.fixed_dictionaries({"and": st.lists(inner, min_size=1, max_size=3)}),
        st.fixed_dictionaries({"or": st.lists(inner, min_size=1, max_size=3)}),
    ),
    max_leaves=6,
)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(tree=trees)
def test_fuzz_definitions_are_valid_or_422(tree: dict[str, Any]) -> None:
    fields.register()
    try:
        defn = definitions.build("candidates.candidate", {"filters": tree}, principal=None)
    except ValidationFailed as exc:
        assert exc.status_code == 422
        assert exc.code.startswith(("semantic_", "segment_"))
        return
    assert isinstance(defn, Definition)
    stored = definitions.stored(defn)
    assert validate(stored).source == "candidates.candidate"  # round trip
