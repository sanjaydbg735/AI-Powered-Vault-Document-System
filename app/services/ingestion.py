import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.errors import AppError, PayloadTooLargeError
from app.models import AnalysisStatus, Document, DocumentStatus
from app.services.file_validation import detect_type, sanitize_filename
from app.services.storage import get_storage, storage_key
from app.workers.queue import enqueue_ingest

logger = logging.getLogger(__name__)

READ_CHUNK = 1024 * 1024


@dataclass(slots=True)
class UploadOutcome:
    document: Document
    duplicate: bool


async def _spool(upload: UploadFile, dest: Path) -> tuple[str, int]:
    """Copy the upload to disk in 1 MiB chunks, hashing as we go; constant memory, early abort on size."""
    hasher, size, limit = hashlib.sha256(), 0, settings.max_upload_bytes
    fh = await run_in_threadpool(dest.open, "wb")
    try:
        while chunk := await upload.read(READ_CHUNK):
            size += len(chunk)
            if size > limit:
                raise PayloadTooLargeError(
                    f"File exceeds the {settings.max_upload_mb} MB limit", details={"limit_bytes": limit}
                )
            hasher.update(chunk)
            await run_in_threadpool(fh.write, chunk)
    finally:
        await run_in_threadpool(fh.close)
    if size == 0:
        raise AppError("Uploaded file is empty", code="empty_file")
    return hasher.hexdigest(), size


async def _find_duplicate(db: AsyncSession, owner_id: str, sha: str) -> Document | None:
    return await db.scalar(select(Document).where(Document.owner_id == owner_id, Document.sha256 == sha))


async def ingest_upload(db: AsyncSession, upload: UploadFile, owner_id: str) -> UploadOutcome:
    storage = get_storage()
    filename = sanitize_filename(upload.filename)
    tmp = storage.temp_path()
    try:
        sha, size = await _spool(upload, tmp)
        detected = await run_in_threadpool(detect_type, tmp, filename)

        if existing := await _find_duplicate(db, owner_id, sha):
            return UploadOutcome(existing, duplicate=True)

        previous = await db.scalar(
            select(Document)
            .where(Document.owner_id == owner_id, Document.filename == filename, Document.is_latest.is_(True))
            .with_for_update()
        )
        key = storage_key(owner_id, sha, detected.file_type)
        doc = Document(
            owner_id=owner_id,
            filename=filename,
            content_type=detected.mime,
            file_type=detected.file_type,
            size_bytes=size,
            sha256=sha,
            storage_key=key,
            status=DocumentStatus.QUEUED,
            analysis_status=AnalysisStatus.PENDING,
            version=(previous.version + 1) if previous else 1,
            previous_version_id=previous.id if previous else None,
            tags=[],
        )
        if previous:
            await db.execute(update(Document).where(Document.id == previous.id).values(is_latest=False))
        db.add(doc)
        try:
            await db.flush()
        except IntegrityError:
            # Concurrent upload of identical content won the race – return that one.
            await db.rollback()
            if existing := await _find_duplicate(db, owner_id, sha):
                return UploadOutcome(existing, duplicate=True)
            raise
        await run_in_threadpool(storage.commit, tmp, key)
        # Commit before enqueueing, otherwise the worker may not see the row yet.
        await db.commit()
    finally:
        tmp.unlink(missing_ok=True)

    if not await run_in_threadpool(enqueue_ingest, doc.id):
        doc.status = DocumentStatus.FAILED
        doc.error_code = "queue_unavailable"
        doc.error_message = "Processing queue is unavailable; retry with POST /documents/{id}/reprocess"
        await db.commit()
    logger.info("document uploaded", extra={"document_id": str(doc.id), "size": size, "type": doc.file_type})
    return UploadOutcome(doc, duplicate=False)
