import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.db.session import sync_session
from app.models import Document, DocumentChunk, ProcessingEvent
from app.services.llm import AIProviderError
from app.workers import pipeline
from tests.conftest import ALICE, CONTRACT, upload


@pytest.fixture
def no_inline(monkeypatch):
    monkeypatch.setattr("app.services.ingestion.enqueue_ingest", lambda _id: True)


async def _queued_doc(client) -> uuid.UUID:
    return uuid.UUID((await upload(client, "contract.md", CONTRACT)).json()["document"]["id"])


async def test_ingest_is_idempotent(client, no_inline):
    doc_id = await _queued_doc(client)
    assert pipeline.ingest(doc_id) is True
    assert pipeline.ingest(doc_id) is False  # redelivered task is a no-op
    with sync_session() as db:
        db.get(Document, doc_id).status = "queued"  # simulate manual reprocess
    assert pipeline.ingest(doc_id) is True
    with sync_session() as db:
        n = db.scalar(select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == doc_id))
        assert n == db.get(Document, doc_id).chunk_count  # chunks replaced, not duplicated


async def test_transient_failure_requeues_then_fails_on_final_attempt(client, no_inline, monkeypatch):
    doc_id = await _queued_doc(client)

    def boom(*a, **k):
        raise AIProviderError("RateLimitError: slow down", transient=True)

    monkeypatch.setattr("app.workers.pipeline.embed_documents_sync", boom)

    with pytest.raises(AIProviderError):
        pipeline.ingest(doc_id, attempt=1, final_attempt=False)
    with sync_session() as db:
        doc = db.get(Document, doc_id)
        assert doc.status == "queued" and "retrying" in doc.error_message

    with pytest.raises(AIProviderError):
        pipeline.ingest(doc_id, attempt=4, final_attempt=True)
    with sync_session() as db:
        doc = db.get(Document, doc_id)
        assert doc.status == "failed" and doc.error_code == "embed_failed"
        statuses = list(db.scalars(select(ProcessingEvent.status).where(ProcessingEvent.stage == "embed")))
        assert statuses == ["retry", "failed"]


async def test_analysis_failure_keeps_document_chat_ready(client, no_inline, monkeypatch):
    doc_id = await _queued_doc(client)
    assert pipeline.ingest(doc_id)

    def boom(*a, **k):
        raise AIProviderError("APIConnectionError", transient=True)

    monkeypatch.setattr("app.workers.pipeline.analyze_pages", boom)
    with pytest.raises(AIProviderError):
        pipeline.analyze(doc_id, final_attempt=True)

    doc = (await client.get(f"/documents/{doc_id}", headers=ALICE)).json()
    assert doc["status"] == "ready" and doc["analysis_status"] == "failed"


async def test_in_flight_claim_is_respected_until_stale(client, no_inline):
    doc_id = await _queued_doc(client)
    with sync_session() as db:
        doc = db.get(Document, doc_id)
        doc.status, doc.processing_started_at = "processing", datetime.now(timezone.utc)
    assert pipeline.ingest(doc_id) is False  # another worker holds a fresh claim

    with sync_session() as db:
        db.get(Document, doc_id).processing_started_at = datetime.now(timezone.utc) - timedelta(
            seconds=pipeline.STALE_PROCESSING_S + 1
        )
    assert pipeline.ingest(doc_id) is True  # crashed worker's claim expired


async def test_deleted_document_is_skipped(no_inline):
    assert pipeline.ingest(uuid.uuid4()) is False
