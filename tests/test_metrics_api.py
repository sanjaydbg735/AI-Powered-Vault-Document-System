from tests.conftest import ALICE, BOB, CONTRACT, HANDBOOK, upload


async def test_document_and_processing_metrics(client):
    await upload(client, "contract.md", CONTRACT)
    await upload(client, "handbook.txt", HANDBOOK)
    await upload(client, "blank.txt", b"   ")  # passes upload validation, fails extraction (no_text)

    docs = (await client.get("/metrics/documents", headers=ALICE)).json()
    assert docs["total_documents"] == 3
    assert docs["by_status"] == {"ready": 2, "failed": 1}
    assert docs["by_file_type"]["txt"] == 2

    proc = (await client.get("/metrics/processing", headers=ALICE)).json()
    assert proc["stages"]["embed"]["success"] == 2
    assert proc["stages"]["extract"]["failed"] == 1
    assert proc["end_to_end_ms"]["documents_processed"] == 2
    assert proc["recent_failures"][0]["error_code"] == "no_text"

    # metrics are tenant-scoped
    assert (await client.get("/metrics/documents", headers=BOB)).json()["total_documents"] == 0


async def test_usage_metrics_track_operations(client):
    await upload(client, "contract.md", CONTRACT)
    sid = (await client.post("/chat/sessions", json={}, headers=ALICE)).json()["id"]
    for _ in range(2):  # second identical question hits the embedding cache
        await client.post(f"/chat/sessions/{sid}/messages", json={"question": "When are invoices due?"}, headers=ALICE)
    usage = (await client.get("/metrics/usage", headers=ALICE)).json()
    ops = {(b["operation"]): b for b in usage["breakdown"]}
    assert {"embed_document", "analysis", "chat_answer", "embed_query"} <= set(ops)
    assert ops["embed_query"]["cache_hits"] == 1
    assert usage["chat"]["answers"] == 2 and usage["chat"]["grounded_rate"] == 1.0


async def test_health(client):
    assert (await client.get("/health/live")).json() == {"status": "ok"}
    r = await client.get("/health/ready")
    assert r.status_code == 200 and r.json()["checks"]["database"]["ok"]
