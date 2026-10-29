import logging
import uuid

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def enqueue_ingest(document_id: uuid.UUID) -> bool:
    """Returns False instead of raising so callers can mark the document and let the user retry."""
    try:
        celery_app.send_task("vault.ingest_document", args=[str(document_id)])
        return True
    except Exception as exc:  # broker unreachable
        logger.error("enqueue failed", extra={"document_id": str(document_id), "error": str(exc)})
        return False


def enqueue_analysis(document_id: uuid.UUID) -> bool:
    try:
        celery_app.send_task("vault.analyze_document", args=[str(document_id)])
        return True
    except Exception as exc:
        logger.error("enqueue analysis failed", extra={"document_id": str(document_id), "error": str(exc)})
        return False
