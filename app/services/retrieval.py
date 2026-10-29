"""Hybrid retrieval: pgvector ANN + Postgres full-text, fused with Reciprocal Rank Fusion.

Everything happens in ONE SQL round trip. Tenant and document filters are applied
inside both candidate queries, so we never retrieve another owner's chunks.
"""

import re
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import Float, cast, func, literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Document, DocumentChunk, DocumentStatus
from app.services.embeddings import embed_query

_TERM = re.compile(r"[A-Za-z0-9]+")


@dataclass(slots=True)
class RetrievedChunk:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    content: str
    page_start: int | None
    page_end: int | None
    token_count: int
    score: float
    vector_rank: int | None
    keyword_rank: int | None

    @property
    def pages(self) -> str | None:
        if self.page_start is None:
            return None
        return str(self.page_start) if self.page_start == self.page_end else f"{self.page_start}-{self.page_end}"


def or_tsquery(query: str) -> str | None:
    """OR the terms: natural-language questions rarely contain *all* words of the passage
    (websearch_to_tsquery would AND them). ts_rank_cd still favours chunks matching more terms."""
    terms = list(dict.fromkeys(t.lower() for t in _TERM.findall(query) if len(t) > 1))[:32]
    return " | ".join(terms) if terms else None


async def hybrid_search(
    db: AsyncSession,
    query: str,
    *,
    owner_id: str,
    document_ids: list[uuid.UUID] | None = None,
    top_k: int | None = None,
    session_id: uuid.UUID | None = None,
) -> tuple[list[RetrievedChunk], int]:
    start = time.perf_counter()
    top_k = top_k or settings.retrieval_top_k
    n = max(settings.retrieval_candidates, top_k)
    k = settings.rrf_k

    q_vec = await embed_query(db, query, owner_id=owner_id, session_id=session_id)

    # Larger candidate list + iterative scan so filtered HNSW queries still return n rows.
    await db.execute(text("SET LOCAL hnsw.ef_search = 100"))
    await db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))

    filters = [DocumentChunk.owner_id == owner_id, Document.status == DocumentStatus.READY]
    if document_ids:
        filters.append(DocumentChunk.document_id.in_(document_ids))
    else:
        # "All documents" means the current version of each; old versions only when picked explicitly.
        filters.append(Document.is_latest.is_(True))

    distance = DocumentChunk.embedding.cosine_distance(q_vec)
    vec = (
        select(DocumentChunk.id.label("id"), func.row_number().over(order_by=distance).label("rank"))
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(*filters)
        .order_by(distance)
        .limit(n)
        .cte("vec")
    )

    tsq_text = or_tsquery(query)
    if tsq_text:
        tsq = func.to_tsquery("english", tsq_text)
        rank_expr = func.ts_rank_cd(DocumentChunk.tsv, tsq)
        kw = (
            select(DocumentChunk.id.label("id"), func.row_number().over(order_by=rank_expr.desc()).label("rank"))
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(*filters, DocumentChunk.tsv.op("@@")(tsq))
            .order_by(rank_expr.desc())
            .limit(n)
            .cte("kw")
        )
    else:
        kw = select(cast(literal(None), DocumentChunk.id.type).label("id"), literal(0).label("rank")).where(
            literal(False)
        ).cte("kw")

    score = func.coalesce(1.0 / (k + vec.c.rank), 0.0) + func.coalesce(1.0 / (k + kw.c.rank), 0.0)
    fused = (
        select(
            func.coalesce(vec.c.id, kw.c.id).label("id"),
            cast(score, Float).label("score"),
            vec.c.rank.label("vector_rank"),
            kw.c.rank.label("keyword_rank"),
        )
        .select_from(vec.join(kw, vec.c.id == kw.c.id, full=True))
        .order_by(score.desc())
        .limit(top_k)
        .subquery("fused")
    )
    stmt = (
        select(DocumentChunk, Document.filename, fused.c.score, fused.c.vector_rank, fused.c.keyword_rank)
        .join(fused, fused.c.id == DocumentChunk.id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .order_by(fused.c.score.desc())
    )
    rows = (await db.execute(stmt)).all()
    hits = [
        RetrievedChunk(
            chunk_id=c.id,
            document_id=c.document_id,
            filename=filename,
            chunk_index=c.chunk_index,
            content=c.content,
            page_start=c.page_start,
            page_end=c.page_end,
            token_count=c.token_count,
            score=round(s, 6),
            vector_rank=vr,
            keyword_rank=kr,
        )
        for c, filename, s, vr, kr in rows
    ]
    return hits, int((time.perf_counter() - start) * 1000)
