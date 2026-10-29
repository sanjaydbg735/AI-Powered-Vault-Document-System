import logging
import uuid

from app.workers import pipeline
from app.workers.celery_app import celery_app
from app.workers.queue import enqueue_analysis

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


def _backoff(retries: int) -> int:
    return min(10 * 2**retries, 300)  # 10s, 20s, 40s ... capped at 5 min


@celery_app.task(name="vault.ingest_document", bind=True, max_retries=MAX_RETRIES)
def ingest_document(self, document_id: str) -> None:
    attempt = self.request.retries + 1
    try:
        ready = pipeline.ingest(uuid.UUID(document_id), attempt=attempt, final_attempt=attempt > MAX_RETRIES)
    except Exception as exc:
        if attempt > MAX_RETRIES:
            return  # pipeline already marked the document FAILED
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries))
    if ready:
        enqueue_analysis(uuid.UUID(document_id))


@celery_app.task(name="vault.analyze_document", bind=True, max_retries=MAX_RETRIES)
def analyze_document(self, document_id: str) -> None:
    attempt = self.request.retries + 1
    try:
        pipeline.analyze(uuid.UUID(document_id), attempt=attempt, final_attempt=attempt > MAX_RETRIES)
    except Exception as exc:
        if attempt > MAX_RETRIES:
            return
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries))
