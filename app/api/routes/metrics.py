from datetime import datetime, timedelta, timezone
from typing import Annotated

import redis
from fastapi import APIRouter, Query
from sqlalchemy import Float, Numeric, cast, extract, func, select

from app.api.deps import DB, Owner
from app.models import ChatMessage, ChatSession, Document, DocumentStatus, LLMUsage, ProcessingEvent
from app.services.cache import get_async_redis

router = APIRouter(prefix="/metrics", tags=["metrics"])

Window = Annotated[int, Query(ge=1, le=24 * 90, description="Look-back window in hours")]


def _since(hours: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _pct(col, p: float):
    return func.round(cast(func.percentile_cont(p).within_group(col), Numeric), 1)


@router.get("/documents", summary="Document statistics")
async def document_stats(db: DB, owner: Owner):
    mine = Document.owner_id == owner
    totals = (
        await db.execute(
            select(
                func.count(),
                func.coalesce(func.sum(Document.size_bytes), 0),
                func.coalesce(func.sum(Document.chunk_count), 0),
                func.coalesce(func.sum(Document.page_count), 0),
                func.coalesce(func.sum(Document.token_count), 0),
                func.count().filter(Document.is_latest.is_(False)),
            ).where(mine)
        )
    ).one()

    async def group(col):
        rows = await db.execute(select(col, func.count()).where(mine).group_by(col).order_by(func.count().desc()))
        return {str(k) if k is not None else "uncategorized": v for k, v in rows.all()}

    tag = func.unnest(Document.tags).label("tag")
    tags_q = select(tag, func.count()).where(mine).group_by("tag").order_by(func.count().desc()).limit(15)
    day = func.date_trunc("day", Document.created_at).label("day")
    daily = await db.execute(
        select(day, func.count()).where(mine, Document.created_at >= _since(24 * 14)).group_by("day").order_by("day")
    )
    return {
        "total_documents": totals[0],
        "total_size_bytes": totals[1],
        "total_chunks": totals[2],
        "total_pages": totals[3],
        "total_tokens": totals[4],
        "superseded_versions": totals[5],
        "by_status": await group(Document.status),
        "by_analysis_status": await group(Document.analysis_status),
        "by_category": await group(Document.category),
        "by_file_type": await group(Document.file_type),
        "top_tags": {t: c for t, c in (await db.execute(tags_q)).all()},
        "uploads_last_14_days": [{"day": d.date().isoformat(), "count": c} for d, c in daily.all()],
    }


@router.get("/processing", summary="Pipeline performance: stage latencies, failure rates, queue depth")
async def processing_metrics(db: DB, owner: Owner, window_hours: Window = 24):
    since = _since(window_hours)
    ev = [ProcessingEvent.owner_id == owner, ProcessingEvent.created_at >= since]
    stages = await db.execute(
        select(
            ProcessingEvent.stage,
            func.count(),
            func.count().filter(ProcessingEvent.status == "success"),
            func.count().filter(ProcessingEvent.status == "failed"),
            func.count().filter(ProcessingEvent.status == "retry"),
            func.avg(ProcessingEvent.duration_ms),
            _pct(ProcessingEvent.duration_ms, 0.5),
            _pct(ProcessingEvent.duration_ms, 0.95),
            func.max(ProcessingEvent.duration_ms),
        )
        .where(*ev)
        .group_by(ProcessingEvent.stage)
    )
    by_stage = {
        s: {
            "runs": n,
            "success": ok,
            "failed": failed,
            "retries": retries,
            "success_rate": round(ok / n, 4) if n else None,
            "avg_ms": round(float(avg), 1) if avg is not None else None,
            "p50_ms": p50,
            "p95_ms": p95,
            "max_ms": mx,
        }
        for s, n, ok, failed, retries, avg, p50, p95, mx in stages.all()
    }

    # End-to-end: upload -> chat-ready, and upload -> analysed
    to_ready = extract("epoch", Document.processed_at - Document.created_at) * 1000
    to_analyzed = extract("epoch", Document.analyzed_at - Document.created_at) * 1000
    e2e = (
        await db.execute(
            select(
                cast(_pct(to_ready, 0.5), Float),
                cast(_pct(to_ready, 0.95), Float),
                cast(_pct(to_analyzed, 0.5), Float),
                cast(_pct(to_analyzed, 0.95), Float),
                func.count().filter(Document.processed_at.is_not(None)),
            ).where(Document.owner_id == owner, Document.created_at >= since)
        )
    ).one()

    in_flight = await db.execute(
        select(Document.status, func.count())
        .where(Document.owner_id == owner, Document.status.in_([DocumentStatus.QUEUED, DocumentStatus.PROCESSING]))
        .group_by(Document.status)
    )
    failures = await db.execute(
        select(Document.id, Document.filename, Document.error_code, Document.error_message, Document.updated_at)
        .where(Document.owner_id == owner, Document.status == DocumentStatus.FAILED)
        .order_by(Document.updated_at.desc())
        .limit(10)
    )
    try:
        r = get_async_redis()
        queue_depth = {"ingest": await r.llen("ingest"), "analysis": await r.llen("analysis")}
    except (redis.RedisError, OSError):
        queue_depth = None

    return {
        "window_hours": window_hours,
        "stages": by_stage,
        "end_to_end_ms": {
            "upload_to_ready_p50": e2e[0],
            "upload_to_ready_p95": e2e[1],
            "upload_to_analyzed_p50": e2e[2],
            "upload_to_analyzed_p95": e2e[3],
            "documents_processed": e2e[4],
        },
        "in_flight": dict(in_flight.all()),
        "queue_depth": queue_depth,
        "recent_failures": [
            {"document_id": str(i), "filename": f, "error_code": c, "error": (m or "")[:300], "at": t}
            for i, f, c, m, t in failures.all()
        ],
    }


@router.get("/usage", summary="AI usage, tokens and cost (USD)")
async def usage_metrics(db: DB, owner: Owner, window_hours: Window = 24 * 30):
    since = _since(window_hours)
    flt = [LLMUsage.owner_id == owner, LLMUsage.created_at >= since]
    rows = await db.execute(
        select(
            LLMUsage.operation,
            LLMUsage.model,
            func.count(),
            func.count().filter(LLMUsage.cached.is_(True)),
            func.sum(LLMUsage.prompt_tokens),
            func.sum(LLMUsage.completion_tokens),
            func.sum(LLMUsage.cost_usd),
            func.avg(LLMUsage.latency_ms).filter(LLMUsage.cached.is_(False)),
        )
        .where(*flt)
        .group_by(LLMUsage.operation, LLMUsage.model)
        .order_by(func.sum(LLMUsage.cost_usd).desc())
    )
    breakdown = [
        {
            "operation": op,
            "model": model,
            "calls": calls,
            "cache_hits": hits,
            "prompt_tokens": int(pt or 0),
            "completion_tokens": int(ct or 0),
            "cost_usd": float(cost or 0),
            "avg_latency_ms": round(float(lat), 1) if lat is not None else None,
        }
        for op, model, calls, hits, pt, ct, cost, lat in rows.all()
    ]
    chat = (
        await db.execute(
            select(
                func.count(),
                _pct(ChatMessage.latency_ms, 0.5),
                _pct(ChatMessage.latency_ms, 0.95),
                func.count().filter(func.jsonb_array_length(ChatMessage.citations) > 0),
            )
            .join(ChatSession, ChatSession.id == ChatMessage.session_id)
            .where(ChatSession.owner_id == owner, ChatMessage.role == "assistant", ChatMessage.created_at >= since)
        )
    ).one()
    total_cost = sum(b["cost_usd"] for b in breakdown)
    docs = await db.scalar(
        select(func.count()).where(Document.owner_id == owner, Document.created_at >= since)
    )
    return {
        "window_hours": window_hours,
        "total_cost_usd": round(total_cost, 6),
        "total_calls": sum(b["calls"] for b in breakdown),
        "total_tokens": sum(b["prompt_tokens"] + b["completion_tokens"] for b in breakdown),
        "avg_cost_per_document_usd": round(
            sum(b["cost_usd"] for b in breakdown if b["operation"] in ("embed_document", "analysis", "section_notes"))
            / docs,
            6,
        )
        if docs
        else None,
        "chat": {
            "answers": chat[0],
            "latency_p50_ms": chat[1],
            "latency_p95_ms": chat[2],
            "grounded_rate": round(chat[3] / chat[0], 4) if chat[0] else None,
        },
        "breakdown": breakdown,
    }
