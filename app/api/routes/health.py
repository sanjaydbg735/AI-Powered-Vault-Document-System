import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.services.cache import get_async_redis

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", summary="Liveness: the process is up")
async def live():
    return {"status": "ok"}


@router.get("/ready", summary="Readiness: dependencies reachable (no auth, for load balancers)")
async def ready():
    checks: dict[str, dict] = {}
    for name, probe in (("database", _db), ("redis", _redis)):
        start = time.perf_counter()
        try:
            await probe()
            checks[name] = {"ok": True, "latency_ms": round((time.perf_counter() - start) * 1000, 1)}
        except Exception as exc:
            checks[name] = {"ok": False, "error": type(exc).__name__}
    healthy = all(c["ok"] for c in checks.values())
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks, "llm_provider": settings.llm_provider},
        status_code=200 if healthy else 503,
    )


async def _db():
    async with AsyncSessionLocal() as s:
        await s.execute(text("SELECT 1"))


async def _redis():
    await get_async_redis().ping()
