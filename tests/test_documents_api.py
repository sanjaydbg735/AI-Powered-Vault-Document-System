import uuid

from sqlalchemy import func, select

from app.core.config import settings
from app.db.session import sync_session
from app.models import DocumentChunk, LLMUsage, ProcessingEvent
from tests.conftest import ALICE, BOB, CONTRACT, HANDBOOK, upload


async def test_upload_processes_to_ready_with_insights(client):
    r = await upload(client, "contract.md", CONTRACT)
    assert r.status_code == 202
    body = r.json()
    assert body["duplicate"] is False
    doc_id = body["document"]["id"]
    assert r.headers["location"].endswith(doc_id)

    doc = (await client.get(f"/documents/{doc_id}", headers=ALICE)).json()
    assert doc["status"] == "ready"
    assert doc["analysis_status"] == "completed"
    assert doc["chunk_count"] >= 1
    assert doc["category"] == "legal"

    ins = (await client.get(f"/documents/{doc_id}/insights", headers=ALICE)).json()
    assert ins["summary"] and ins["key_points"] and ins["suggested_questions"]
    assert ins["prompt_version"]


async def test_pipeline_records_stage_events_and_usage(client):
    doc_id = (await upload(client, "contract.md", CONTRACT)).json()["document"]["id"]
    with sync_session() as db:
        stages = set(db.scalars(select(ProcessingEvent.stage).where(ProcessingEvent.document_id == uuid.UUID(doc_id))))
        ops = set(db.scalars(select(LLMUsage.operation).where(LLMUsage.document_id == uuid.UUID(doc_id))))
    assert {"extract", "chunk", "embed", "index", "pipeline", "analyze"} <= stages
    assert {"embed_document", "analysis"} <= ops


async def test_duplicate_upload_returns_existing_without_reprocessing(client, inline_worker):
    first = (await upload(client, "a.md", CONTRACT)).json()["document"]["id"]
    r = await upload(client, "renamed.md", CONTRACT)
    assert r.status_code == 200
    assert r.json()["duplicate"] is True
    assert r.json()["document"]["id"] == first
    assert len(inline_worker) == 1


async def test_same_filename_new_content_creates_version(client):
    v1 = (await upload(client, "policy.md", CONTRACT)).json()["document"]
    v2 = (await upload(client, "policy.md", CONTRACT + "\n## Amendment\nNotice is now 60 days.\n")).json()["document"]
    assert v2["version"] == 2 and v2["previous_version_id"] == v1["id"]

    listed = (await client.get("/documents", headers=ALICE)).json()
    assert [d["id"] for d in listed["items"]] == [v2["id"]]
    all_versions = (await client.get("/documents?include_old_versions=true", headers=ALICE)).json()
    assert all_versions["total"] == 2

    # deleting the latest version promotes the previous one
    assert (await client.delete(f"/documents/{v2['id']}", headers=ALICE)).status_code == 204
    assert (await client.get(f"/documents/{v1['id']}", headers=ALICE)).json()["is_latest"] is True


async def test_rejects_bad_files(client):
    r = await upload(client, "evil.exe", b"MZ\x90\x00")
    assert r.status_code == 415 and r.json()["error"]["code"] == "unsupported_media_type"

    r = await upload(client, "fake.pdf", b"this is not a pdf")
    assert r.status_code == 415

    r = await upload(client, "empty.txt", b"")
    assert r.status_code == 400 and r.json()["error"]["code"] == "empty_file"


async def test_rejects_oversized_file(client, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    r = await upload(client, "big.txt", b"a" * (1024 * 1024 + 10))
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"


async def test_unparseable_content_marks_failed_and_can_be_reprocessed(client):
    r = await upload(client, "blank.txt", b"   \n  hi \n")
    doc = r.json()["document"]
    detail = (await client.get(f"/documents/{doc['id']}", headers=ALICE)).json()
    assert detail["status"] == "failed"
    assert detail["error_code"] == "no_text"

    r = await client.post(f"/documents/{doc['id']}/reprocess", headers=ALICE)
    assert r.status_code == 202


async def test_batch_upload_partial_success(client):
    files = [
        ("files", ("ok.md", CONTRACT.encode())),
        ("files", ("bad.exe", b"MZ")),
        ("files", ("hb.txt", HANDBOOK.encode())),
    ]
    r = await client.post("/documents/batch", files=files, headers=ALICE)
    assert r.status_code == 207
    body = r.json()
    assert body["accepted"] == 2 and body["failed"] == 1
    assert body["items"][1]["error"]["code"] == "unsupported_media_type"


async def test_list_filters_and_pagination(client):
    await upload(client, "contract.md", CONTRACT)
    await upload(client, "handbook.txt", HANDBOOK)
    page = (await client.get("/documents?limit=1", headers=ALICE)).json()
    assert page["total"] == 2 and len(page["items"]) == 1
    legal = (await client.get("/documents?category=legal", headers=ALICE)).json()
    assert [d["filename"] for d in legal["items"]] == ["contract.md"]
    q = (await client.get("/documents?q=handb", headers=ALICE)).json()
    assert [d["filename"] for d in q["items"]] == ["handbook.txt"]
    r = await client.get("/documents?limit=1000", headers=ALICE)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


async def test_tenant_isolation(client):
    doc_id = (await upload(client, "contract.md", CONTRACT)).json()["document"]["id"]
    assert (await client.get(f"/documents/{doc_id}", headers=BOB)).status_code == 404
    assert (await client.get("/documents", headers=BOB)).json()["total"] == 0
    hits = (await client.post("/search", json={"query": "terminate notice"}, headers=BOB)).json()["hits"]
    assert hits == []
    assert (await client.get("/documents", headers={"X-API-Key": "nope"})).status_code == 401


async def test_custom_summary_is_cached(client):
    doc_id = (await upload(client, "contract.md", CONTRACT)).json()["document"]["id"]
    body = {"length": "short", "tone": "executive", "focus": "payment terms"}
    first = (await client.post(f"/documents/{doc_id}/summaries", json=body, headers=ALICE)).json()
    second = (await client.post(f"/documents/{doc_id}/summaries", json=body, headers=ALICE)).json()
    assert first["summary"] and first["cached"] is False
    assert second["cached"] is True and second["summary"] == first["summary"]


async def test_compare_documents(client):
    a = (await upload(client, "contract.md", CONTRACT)).json()["document"]["id"]
    b = (await upload(client, "handbook.txt", HANDBOOK)).json()["document"]["id"]
    r = await client.post("/documents/compare", json={"document_ids": [a, b]}, headers=ALICE)
    assert r.status_code == 200
    assert set(r.json()["unique_points"]) == {a, b}
    r = await client.post("/documents/compare", json={"document_ids": [a]}, headers=ALICE)
    assert r.status_code == 422


async def test_hybrid_search_ranks_relevant_document_first(client):
    await upload(client, "contract.md", CONTRACT)
    await upload(client, "handbook.txt", HANDBOOK)
    r = (await client.post("/search", json={"query": "parental leave weeks"}, headers=ALICE)).json()
    assert r["hits"][0]["filename"] == "handbook.txt"
    assert r["hits"][0]["keyword_rank"] == 1


async def test_delete_removes_chunks(client):
    doc_id = (await upload(client, "contract.md", CONTRACT)).json()["document"]["id"]
    assert (await client.delete(f"/documents/{doc_id}", headers=ALICE)).status_code == 204
    assert (await client.get(f"/documents/{doc_id}", headers=ALICE)).status_code == 404
    with sync_session() as db:
        assert db.scalar(select(func.count()).select_from(DocumentChunk)) == 0
