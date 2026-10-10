"""Story 3.7 attachments on candidates (the generic files API with ``candidates`` -> ``candidate``)."""

from tests.epic3 import CANDIDATES, Org3, create_candidate, resume_pdf, upload


async def test_certificate_with_expiry_is_listed_and_downloadable(org3: Org3) -> None:
    cand = await create_candidate(org3.admin)
    url = f"{CANDIDATES}/{cand['id']}/attachments"
    resp = await upload(
        org3.admin.client, url, resume_pdf(), "aws.pdf", category="certificate", expires_on="2028-05-01"
    )
    assert resp.status_code == 201, resp.text
    listed = (await org3.admin.client.get(url)).json()
    assert [(a["category"], a["expires_on"]) for a in listed] == [("certificate", "2028-05-01")]
    dl = await org3.admin.client.get(listed[0]["file"]["download_url"])
    assert dl.status_code == 200
    assert dl.headers["x-content-type-options"] == "nosniff"
    assert (await org3.admin.client.get(f"{CANDIDATES}/{cand['id']}")).json()["counts"]["attachments"] == 1


async def test_bad_category_is_422(org3: Org3) -> None:
    cand = await create_candidate(org3.admin)
    resp = await upload(
        org3.admin.client,
        f"{CANDIDATES}/{cand['id']}/attachments",
        resume_pdf(),
        "x.pdf",
        category="passport_photo",
    )
    assert resp.status_code == 422
