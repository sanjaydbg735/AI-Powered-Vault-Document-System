import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, File, Query, Request, Response, UploadFile, status
from sqlalchemy import func, or_, select
from starlette.concurrency import run_in_threadpool

from app.api.deps import DB, Owner, owned_document
from app.core.config import settings
from app.core.errors import AppError, ConflictError, PayloadTooLargeError
from app.core.rate_limit import ai_limit, upload_limit
from app.models import AnalysisStatus, Document, DocumentChunk, DocumentStatus
from app.schemas.documents import (
    BatchItem,
    BatchUploadResponse,
    ChunkOut,
    CompareOut,
    CompareRequest,
    DocumentOut,
    DocumentPage,
    InsightsOut,
    SearchHit,
    SearchRequest,
    SearchResponse,
    SummaryOut,
    SummaryRequest,
    UploadResponse,
)
from app.services.document_ai import compare_documents, custom_summary
from app.services.ingestion import ingest_upload
from app.services.retrieval import hybrid_search
from app.services.storage import get_storage
from app.workers.queue import enqueue_ingest

router = APIRouter(tags=["documents"])


def _reject_oversized(request: Request, files: int = 1) -> None:
    # Cheap early rejection before the multipart body is parsed.
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > settings.max_upload_bytes * files + 1024 * 64:
        raise PayloadTooLargeError(f"Request exceeds the {settings.max_upload_mb} MB per-file limit")


@router.post(
    "/documents",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[upload_limit],
    summary="Upload a document (PDF, DOCX, TXT, MD)",
    responses={200: {"description": "Duplicate content: existing document returned"}},
)
async def upload_document(request: Request, response: Response, db: DB, owner: Owner, file: UploadFile = File(...)):
    _reject_oversized(request)
    outcome = await ingest_upload(db, file, owner)
    if outcome.duplicate:
        response.status_code = status.HTTP_200_OK
    doc_id = outcome.document.id
    response.headers["Location"] = f"/api/v1/documents/{doc_id}"
    return UploadResponse(
        document=DocumentOut.model_validate(outcome.document),
        duplicate=outcome.duplicate,
        status_url=f"/api/v1/documents/{doc_id}",
    )


@router.post(
    "/documents/batch",
    response_model=BatchUploadResponse,
    status_code=status.HTTP_207_MULTI_STATUS,
    dependencies=[upload_limit],
    summary="Upload several documents; each file succeeds or fails independently",
)
async def upload_batch(request: Request, db: DB, owner: Owner, files: Annotated[list[UploadFile], File(...)]):
    if len(files) > settings.max_batch_files:
        raise AppError(f"At most {settings.max_batch_files} files per batch", code="too_many_files")
    _reject_oversized(request, len(files))
    items: list[BatchItem] = []
    for f in files:
        try:
            outcome = await ingest_upload(db, f, owner)
            items.append(
                BatchItem(
                    filename=f.filename,
                    ok=True,
                    duplicate=outcome.duplicate,
                    document=DocumentOut.model_validate(outcome.document),
                )
            )
        except AppError as exc:
            await db.rollback()
            items.append(BatchItem(filename=f.filename, ok=False, error={"code": exc.code, "message": exc.message}))
    ok = sum(i.ok for i in items)
    return BatchUploadResponse(accepted=ok, failed=len(items) - ok, items=items)


@router.get("/documents", response_model=DocumentPage, summary="List documents with filters")
async def list_documents(
    db: DB,
    owner: Owner,
    status_: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    category: str | None = None,
    tag: str | None = None,
    q: Annotated[str | None, Query(max_length=200, description="Search filename/title")] = None,
    include_old_versions: bool = False,
    sort: Literal["created_at", "-created_at", "filename", "size_bytes"] = "-created_at",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    filters = [Document.owner_id == owner]
    if not include_old_versions:
        filters.append(Document.is_latest.is_(True))
    if status_:
        filters.append(Document.status == status_)
    if category:
        filters.append(Document.category == category.lower())
    if tag:
        filters.append(Document.tags.any(tag.lower()))
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        filters.append(or_(Document.filename.ilike(pattern), Document.title.ilike(pattern)))

    col = getattr(Document, sort.lstrip("-"))
    order = col.desc() if sort.startswith("-") else col.asc()
    total = await db.scalar(select(func.count()).select_from(Document).where(*filters))
    rows = await db.scalars(select(Document).where(*filters).order_by(order, Document.id).limit(limit).offset(offset))
    return DocumentPage(items=[DocumentOut.model_validate(d) for d in rows], total=total or 0, limit=limit, offset=offset)


@router.get("/documents/{document_id}", response_model=DocumentOut, summary="Document detail and processing status")
async def get_document(document_id: uuid.UUID, db: DB, owner: Owner):
    return await owned_document(db, owner, document_id)


@router.get("/documents/{document_id}/insights", response_model=InsightsOut, summary="AI-generated insights")
async def get_insights(document_id: uuid.UUID, db: DB, owner: Owner):
    doc = await owned_document(db, owner, document_id)
    ins = doc.insights or {}
    return InsightsOut(
        document_id=doc.id,
        analysis_status=doc.analysis_status,
        title=doc.title,
        summary=doc.summary,
        category=doc.category,
        tags=doc.tags,
        language=doc.language,
        key_points=ins.get("key_points", []),
        sentiment=ins.get("sentiment"),
        entities=ins.get("entities", []),
        suggested_questions=ins.get("suggested_questions", []),
        model=ins.get("model"),
        prompt_version=ins.get("prompt_version"),
    )


@router.post(
    "/documents/{document_id}/summaries",
    response_model=SummaryOut,
    dependencies=[ai_limit],
    summary="Generate a custom summary (length, tone, focus)",
)
async def create_summary(document_id: uuid.UUID, body: SummaryRequest, db: DB, owner: Owner):
    doc = await owned_document(db, owner, document_id)
    text, cached, model = await custom_summary(db, doc, body)
    return SummaryOut(document_id=doc.id, summary=text, params=body, cached=cached, model=model)


@router.get("/documents/{document_id}/chunks", response_model=list[ChunkOut], summary="Inspect indexed chunks")
async def get_chunks(
    document_id: uuid.UUID,
    db: DB,
    owner: Owner,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    await owned_document(db, owner, document_id)
    rows = await db.scalars(
        select(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.chunk_index)
        .limit(limit)
        .offset(offset)
    )
    return list(rows)


@router.post(
    "/documents/{document_id}/reprocess",
    response_model=DocumentOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-run the processing pipeline (e.g. after a failure)",
)
async def reprocess(document_id: uuid.UUID, db: DB, owner: Owner):
    doc = await owned_document(db, owner, document_id)
    if doc.status == DocumentStatus.PROCESSING:
        raise ConflictError("Document is currently being processed")
    doc.status = DocumentStatus.QUEUED
    doc.analysis_status = AnalysisStatus.PENDING
    doc.error_code = doc.error_message = None
    await db.commit()
    if not await run_in_threadpool(enqueue_ingest, doc.id):
        doc.status = DocumentStatus.FAILED
        doc.error_code = "queue_unavailable"
        doc.error_message = "Processing queue is unavailable; try again shortly"
        await db.commit()
    return doc


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a document")
async def delete_document(document_id: uuid.UUID, db: DB, owner: Owner):
    doc = await owned_document(db, owner, document_id)
    key = doc.storage_key
    if doc.is_latest and doc.previous_version_id:
        prev = await db.get(Document, doc.previous_version_id)
        if prev is not None:
            prev.is_latest = True
    await db.delete(doc)  # chunks cascade in the database
    await db.commit()
    await run_in_threadpool(get_storage().delete, key)


@router.post(
    "/documents/compare", response_model=CompareOut, dependencies=[ai_limit], summary="Compare 2-5 documents with AI"
)
async def compare(body: CompareRequest, db: DB, owner: Owner):
    ids = list(dict.fromkeys(body.document_ids))
    if len(ids) < 2:
        raise AppError("Provide at least two distinct documents", code="validation_error")
    docs = [await owned_document(db, owner, i) for i in ids]
    result, cached = await compare_documents(db, docs, body.focus)
    return CompareOut(
        documents=[{"id": str(d.id), "filename": d.filename} for d in docs], cached=cached, **result.model_dump()
    )


@router.post("/search", response_model=SearchResponse, summary="Hybrid semantic + keyword search across documents")
async def search(body: SearchRequest, db: DB, owner: Owner):
    hits, took = await hybrid_search(db, body.query, owner_id=owner, document_ids=body.document_ids, top_k=body.top_k)
    return SearchResponse(
        query=body.query,
        took_ms=took,
        hits=[
            SearchHit(
                document_id=h.document_id,
                filename=h.filename,
                chunk_index=h.chunk_index,
                pages=h.pages,
                content=h.content,
                score=h.score,
                vector_rank=h.vector_rank,
                keyword_rank=h.keyword_rank,
            )
            for h in hits
        ],
    )
