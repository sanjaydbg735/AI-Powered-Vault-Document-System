"""Processing pipeline, independent of Celery so it is directly testable.

ingest:  extract -> chunk -> embed -> (single transaction) replace chunks, mark READY
analyze: extract -> LLM structured analysis -> store insights

Both steps are idempotent: re-delivery of a task (acks_late) or a manual
reprocess never duplicates chunks or usage.
"""

import logging
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, insert
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.errors import ExtractionError
from app.db.session import sync_session
from app.models import AnalysisStatus, Document, DocumentChunk, DocumentStatus, ProcessingEvent
from app.services import prompts
from app.services.analysis import analyze_pages
from app.services.chunking import chunk_pages
from app.services.embeddings import embed_documents_sync
from app.services.extraction import extract
from app.services.storage import get_storage
from app.services.tokens import count_tokens

logger = logging.getLogger(__name__)

# Must exceed Celery's task_time_limit (15 min) so a live worker's claim is never taken over.
STALE_PROCESSING_S = 16 * 60


class StageTimer:
    def __init__(self, document: Document, attempt: int):
        self.owner_id = document.owner_id
        self.document_id = document.id
        self.attempt = attempt
        self.current = "pipeline"
        self._t0 = time.perf_counter()
        self._pipeline_t0 = self._t0
        self.events: list[ProcessingEvent] = []

    def start(self, stage: str) -> None:
        self.current, self._t0 = stage, time.perf_counter()

    def done(self) -> int:
        ms = int((time.perf_counter() - self._t0) * 1000)
        self.events.append(self._event(self.current, "success", ms))
        return ms

    def total_ms(self) -> int:
        return int((time.perf_counter() - self._pipeline_t0) * 1000)

    def _event(self, stage: str, status: str, ms: int, error: str | None = None) -> ProcessingEvent:
        return ProcessingEvent(
            document_id=self.document_id,
            owner_id=self.owner_id,
            stage=stage,
            status=status,
            duration_ms=ms,
            attempt=self.attempt,
            error=error,
        )

    def failure_event(self, status: str, error: str) -> ProcessingEvent:
        return self._event(self.current, status, int((time.perf_counter() - self._t0) * 1000), error[:2000])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _record_failure(document_id: uuid.UUID, event: ProcessingEvent, *, mark: dict | None) -> None:
    """Separate transaction: the work transaction was rolled back, but the failure must be persisted."""
    with sync_session() as db:
        db.add(event)
        if mark and (doc := db.get(Document, document_id)):
            for k, v in mark.items():
                setattr(doc, k, v)


def ingest(document_id: uuid.UUID, *, attempt: int = 1, final_attempt: bool = True) -> bool:
    """Returns True when the document became READY (caller then schedules analysis)."""
    with sync_session() as db:
        doc = db.get(Document, document_id, with_for_update=True)
        if doc is None:
            logger.info("document vanished before ingest", extra={"document_id": str(document_id)})
            return False
        if doc.status == DocumentStatus.READY:
            return False  # duplicate delivery
        if (
            doc.status == DocumentStatus.PROCESSING
            and doc.processing_started_at
            and (_now() - doc.processing_started_at).total_seconds() < STALE_PROCESSING_S
        ):
            return False  # another worker is on it; a crashed worker's claim expires after STALE_PROCESSING_S
        doc.status = DocumentStatus.PROCESSING
        doc.attempts = attempt
        doc.processing_started_at = _now()
        doc.error_code = doc.error_message = None
        timer = StageTimer(doc, attempt)
        file_type, key, owner_id = doc.file_type, doc.storage_key, doc.owner_id

    try:
        with sync_session() as db:
            timer.start("extract")
            pages = extract(get_storage().local_path(key), file_type)
            timer.done()

            timer.start("chunk")
            chunks = chunk_pages(pages, settings.chunk_size_tokens, settings.chunk_overlap_tokens)
            timer.done()

            timer.start("embed")
            vectors = embed_documents_sync(db, [c.content for c in chunks], owner_id=owner_id, document_id=document_id)
            timer.done()

            timer.start("index")
            db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
            db.execute(
                insert(DocumentChunk),
                [
                    dict(
                        id=uuid.uuid4(),
                        document_id=document_id,
                        owner_id=owner_id,
                        chunk_index=c.index,
                        content=c.content,
                        page_start=c.page_start,
                        page_end=c.page_end,
                        token_count=c.token_count,
                        embedding=v,
                    )
                    for c, v in zip(chunks, vectors, strict=True)
                ],
            )
            doc = db.get(Document, document_id)
            if doc is None:
                raise IntegrityError("document deleted during processing", None, Exception())
            paginated = [p.number for p in pages if p.number is not None]
            doc.page_count = max(paginated) if paginated else None
            doc.char_count = sum(len(p.text) for p in pages)
            doc.token_count = sum(count_tokens(p.text) for p in pages)
            doc.chunk_count = len(chunks)
            doc.status = DocumentStatus.READY
            doc.processed_at = _now()
            timer.done()
            db.add_all(timer.events)
            db.add(timer._event("pipeline", "success", timer.total_ms()))
        logger.info("document indexed", extra={"document_id": str(document_id), "chunks": len(chunks)})
        return True

    except ExtractionError as exc:  # permanent – retrying won't help
        _record_failure(
            document_id,
            timer.failure_event("failed", exc.message),
            mark=dict(status=DocumentStatus.FAILED, error_code=exc.code, error_message=exc.message),
        )
        return False
    except IntegrityError:
        logger.info("document deleted during processing", extra={"document_id": str(document_id)})
        return False
    except Exception as exc:
        logger.exception("ingest failed", extra={"document_id": str(document_id), "stage": timer.current})
        mark = (
            dict(status=DocumentStatus.FAILED, error_code=f"{timer.current}_failed", error_message=str(exc)[:2000])
            if final_attempt
            else dict(status=DocumentStatus.QUEUED, error_message=f"retrying after: {str(exc)[:500]}")
        )
        _record_failure(document_id, timer.failure_event("failed" if final_attempt else "retry", str(exc)), mark=mark)
        raise


def analyze(document_id: uuid.UUID, *, attempt: int = 1, final_attempt: bool = True) -> None:
    with sync_session() as db:
        doc = db.get(Document, document_id, with_for_update=True)
        if doc is None or doc.status != DocumentStatus.READY or doc.analysis_status == AnalysisStatus.COMPLETED:
            return
        doc.analysis_status = AnalysisStatus.PROCESSING
        timer = StageTimer(doc, attempt)
        file_type, key, owner_id, filename = doc.file_type, doc.storage_key, doc.owner_id, doc.filename

    try:
        with sync_session() as db:
            timer.start("analyze")
            pages = extract(get_storage().local_path(key), file_type)
            result = analyze_pages(db, pages, filename=filename, owner_id=owner_id, document_id=document_id)
            doc = db.get(Document, document_id)
            if doc is None:
                return
            doc.title = result.title
            doc.summary = result.summary
            doc.category = result.category
            doc.tags = result.tags
            doc.language = result.language
            doc.insights = {
                "key_points": result.key_points,
                "sentiment": result.sentiment.model_dump(),
                "entities": [e.model_dump() for e in result.entities],
                "suggested_questions": result.suggested_questions,
                "model": "fake" if settings.llm_provider == "fake" else settings.analysis_model,
                "prompt_version": prompts.PROMPT_VERSION,
            }
            doc.analysis_status = AnalysisStatus.COMPLETED
            doc.analyzed_at = _now()
            timer.done()
            db.add_all(timer.events)
    except Exception as exc:
        logger.exception("analysis failed", extra={"document_id": str(document_id)})
        permanent = final_attempt or isinstance(exc, ExtractionError)
        _record_failure(
            document_id,
            timer.failure_event("failed" if permanent else "retry", str(exc)),
            mark=dict(analysis_status=AnalysisStatus.FAILED if permanent else AnalysisStatus.PENDING),
        )
        if not isinstance(exc, ExtractionError):
            raise
