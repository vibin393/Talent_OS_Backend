# ruff: noqa: E501
"""Unified-platform inbound data loops on the Public API v1.

* ``POST /public/v1/assessments`` (Croar -> ATS): resolve by id or external_id; 422 when unknown; idempotent replay.
* ``POST /public/v1/engagement`` (Feedback IQ -> ATS): best-effort email match; stored even when unmatched; idempotent replay.
"""

from fastapi import FastAPI

from tests.epic3 import Org3, create_candidate
from tests.public_api.e67 import Api, code, installed, user_token


def _assessment_body(event_id: str, *, candidate_id: str | None = None, external_id: str | None = None, email: str | None = None) -> dict:
    ref: dict = {}
    if candidate_id is not None:
        ref["id"] = candidate_id
    if external_id is not None:
        ref["external_id"] = external_id
    if email is not None:
        ref["email"] = email
    return {
        "event_id": event_id,
        "candidate_ref": ref,
        "application_ref": None,
        "assessment": {
            "type": "coding", "provider": "croar", "name": "Python screen", "score": 82.5,
            "max_score": 100, "status": "completed", "completed_at": "2026-10-10T10:00:00Z",
            "detail": {"sections": [{"name": "algorithms", "score": 40}]},
        },
    }


async def test_assessment_happy_path_and_replay(app: FastAPI, org3: Org3) -> None:
    cand = await create_candidate(org3.admin, full_name="Asha Rao")
    inst = await installed(org3)
    api = Api(app, await user_token(inst, org3.admin.user_id))

    key = "assess-1"
    resp = await api.post("/assessments", _assessment_body("evt-1", candidate_id=cand["id"]), key=key)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["object"] == "candidate_assessment"
    assert out["candidate_id"] == cand["id"]
    assert out["provider"] == "croar" and out["score"] == 82.5 and out["status"] == "completed"
    assert out["detail"]["sections"][0]["name"] == "algorithms"

    # Idempotency-Key replay returns the stored response
    again = await api.post("/assessments", _assessment_body("evt-1", candidate_id=cand["id"]), key=key)
    assert again.status_code == 201 and again.json()["id"] == out["id"]

    # A fresh Idempotency-Key but the SAME event_id is still idempotent (delayed relay): same row, no duplicate
    relay = await api.post("/assessments", _assessment_body("evt-1", candidate_id=cand["id"]), key="assess-1-relay")
    assert relay.status_code == 201 and relay.json()["id"] == out["id"]


async def test_assessment_resolves_by_external_id(app: FastAPI, org3: Org3) -> None:
    cand = await create_candidate(org3.admin, full_name="Ravi Kumar")
    inst = await installed(org3)
    api = Api(app, await user_token(inst, org3.admin.user_id))
    resp = await api.post("/assessments", _assessment_body("evt-ext", external_id=cand["human_id"]))
    assert resp.status_code == 201, resp.text
    assert resp.json()["candidate_id"] == cand["id"]


async def test_assessment_resolves_by_email(app: FastAPI, org3: Org3) -> None:
    # Croar knows the candidate's email (not the ATS id/human_id) — resolve on it.
    cand = await create_candidate(
        org3.admin, full_name="Nina Shah", emails=[{"email": "nina@example.com", "is_primary": True}]
    )
    inst = await installed(org3)
    api = Api(app, await user_token(inst, org3.admin.user_id))
    resp = await api.post("/assessments", _assessment_body("evt-email", email="NINA@example.com"))
    assert resp.status_code == 201, resp.text
    assert resp.json()["candidate_id"] == cand["id"]
    # an email no candidate has -> 422 (never create from an assessment)
    missing = await api.post("/assessments", _assessment_body("evt-email-x", email="ghost@example.com"))
    assert (missing.status_code, code(missing)) == (422, "candidate_not_found")


async def test_assessment_candidate_not_found_422(app: FastAPI, org3: Org3) -> None:
    inst = await installed(org3)
    api = Api(app, await user_token(inst, org3.admin.user_id))
    import uuid

    missing = await api.post("/assessments", _assessment_body("evt-x", candidate_id=str(uuid.uuid4())))
    assert (missing.status_code, code(missing)) == (422, "candidate_not_found")
    # no ref at all -> 422 (never create a candidate from an assessment)
    none = await api.post("/assessments", _assessment_body("evt-y"))
    assert (none.status_code, code(none)) == (422, "candidate_ref_required")


async def test_engagement_match_store_and_replay(app: FastAPI, org3: Org3) -> None:
    cand = await create_candidate(
        org3.admin, full_name="Meera Nair", emails=[{"email": "meera@example.com", "is_primary": True}]
    )
    inst = await installed(org3)
    api = Api(app, await user_token(inst, org3.admin.user_id))

    def body(event_id: str, email: str) -> dict:
        return {
            "event_id": event_id,
            "subject_ref": {"email": email},
            "signal": {
                "type": "nps", "source": "feedbackiq", "score": 9.0, "summary": "Great onboarding",
                "occurred_at": "2026-10-11T09:00:00Z", "detail": {"campaign": "q4"},
            },
        }

    # matched by email
    key = "eng-1"
    resp = await api.post("/engagement", body("eng-evt-1", "meera@example.com"), key=key)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["object"] == "engagement_signal" and out["matched"] is True
    assert out["matched_candidate_id"] == cand["id"] and out["source"] == "feedbackiq"

    # Idempotency-Key replay
    again = await api.post("/engagement", body("eng-evt-1", "meera@example.com"), key=key)
    assert again.status_code == 201 and again.json()["id"] == out["id"]

    # unmatched subject is STILL stored (201), just matched=False
    unmatched = await api.post("/engagement", body("eng-evt-2", "nobody@example.com"))
    assert unmatched.status_code == 201, unmatched.text
    body2 = unmatched.json()
    assert body2["matched"] is False and body2["matched_candidate_id"] is None
    assert body2["subject_email"] == "nobody@example.com"
