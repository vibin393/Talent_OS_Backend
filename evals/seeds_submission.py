# ruff: noqa: E501
"""Epic 61 eval seeds (case ``subject: seed``): a shortlisted Application on the eval org with the Submission Agent
enabled (``tests/modules/submission_packs/helpers.py``). Placeholders: ``{{application_id}}``, ``{{candidate_id}}``,
``{{demand_id}}``, ``{{client_id}}``, ``{{random_id}}``.

| Seed | State |
|---|---|
| ``submission_shortlisted`` | parsed resume, notice 30 days, expected pay 1000/h (margin passes), tenant attestation mode |
| ``submission_consent_required`` | Client rule ``require_candidate_consent`` -> consent blocks |
| ``submission_unparsed_client_format`` | Client rule ``client_formatted``, latest resume not parsed -> resume_format blocks |
| ``submission_below_margin`` | expected pay 1250/h (capped at the 1100 pay max) -> margin below the 15 % minimum |
| ``submission_duplicate`` | sent to the same Client from another Demand -> duplicate blocks |
| ``submission_not_shortlisted`` | the Application was moved back to an earlier stage before the run |
| ``submission_short_limit`` | Client rule ``write_up_max_words = 40`` |
"""

from typing import Any

from evals.fixtures import register_async_builder


async def _base(org: Any, **kw: Any) -> Any:
    from tests.modules.submission_packs.helpers import enable_agent, seed_pack

    seed = await seed_pack(org.admin, org.tenant_id, **kw)
    await enable_agent(org.tenant_id)
    return seed


async def shortlisted(org: Any) -> dict[str, str]:
    return (await _base(org)).values()


async def consent_required(org: Any) -> dict[str, str]:
    from tests.modules.submission_packs.helpers import put_rules

    seed = await _base(org)
    await put_rules(seed, require_candidate_consent=True)
    return seed.values()


async def unparsed_client_format(org: Any) -> dict[str, str]:
    from tests.modules.submission_packs.helpers import put_rules

    seed = await _base(org, parsed=False)
    await put_rules(seed, resume_format="client_formatted")
    return seed.values()


async def below_margin(org: Any) -> dict[str, str]:
    from tests.modules.submission_packs.helpers import set_candidate

    seed = await _base(org)
    await set_candidate(seed.tenant_id, seed.candidate_id, expected_ctc_amount=1250)
    return seed.values()


async def duplicate(org: Any) -> dict[str, str]:
    from tests.modules.demands.helpers import open_demand
    from tests.modules.pipeline.helpers import POrg, add_one, per_app, shortlist, submit
    from tests.modules.submission_packs.helpers import PackSeed, enable_agent

    first = await _base(org)
    await enable_agent(org.tenant_id, False)  # the second shortlist must not start a run
    porg = POrg(
        tenant=None, admin=first.admin, client_id=first.client_id, contact_id=first.contact_id, contact_email="",  # type: ignore[arg-type]
        demand={"id": str(first.demand_id)},
    )  # fmt: skip
    per = {
        str(first.application_id): per_app(
            consent_attestation={"text": "Agreed by phone today", "confirmed": True}
        )
    }
    resp = await submit(porg, [str(first.application_id)], per)
    assert resp.status_code == 200, resp.text
    d2 = await open_demand(first.admin, first.client_id, title="Data Engineer II")
    app2 = await add_one(first.admin, str(d2["id"]), str(first.candidate_id))
    await shortlist(first.admin, app2)
    await enable_agent(org.tenant_id)
    import uuid

    second = PackSeed(
        tenant_id=first.tenant_id, admin=first.admin, client_id=first.client_id, contact_id=first.contact_id,
        demand_id=uuid.UUID(str(d2["id"])), candidate_id=first.candidate_id, application_id=uuid.UUID(app2),
    )  # fmt: skip
    return second.values()


async def not_shortlisted(org: Any) -> dict[str, str]:
    from tests.modules.pipeline.helpers import move, stages

    seed = await _base(org)
    ids = await stages(seed.admin)
    resp = await move(
        seed.admin, str(seed.application_id), ids["withdrawn"], reason_code="accepted_other_offer"
    )
    assert resp.status_code == 200, resp.text
    return seed.values()


async def short_limit(org: Any) -> dict[str, str]:
    from tests.modules.submission_packs.helpers import put_rules

    seed = await _base(org)
    await put_rules(seed, write_up_max_words=40)
    return seed.values()


for _name, _fn in (
    ("submission_shortlisted", shortlisted),
    ("submission_consent_required", consent_required),
    ("submission_unparsed_client_format", unparsed_client_format),
    ("submission_below_margin", below_margin),
    ("submission_duplicate", duplicate),
    ("submission_not_shortlisted", not_shortlisted),
    ("submission_short_limit", short_limit),
):
    register_async_builder(_name, _fn)
