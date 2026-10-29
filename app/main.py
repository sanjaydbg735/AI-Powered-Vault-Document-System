import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import chat, documents, health, metrics
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, request_id_var
from app.db.session import async_engine
from app.services.storage import get_storage

logger = logging.getLogger("vault.access")

DESCRIPTION = """
Upload documents, get AI insights, and chat with them (RAG with citations).

**Auth:** send `X-API-Key` (see `API_KEYS` env).
**Flow:** `POST /documents` → poll `GET /documents/{id}` until `status=ready` →
`POST /chat/sessions` → `POST /chat/sessions/{id}/messages`.
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging(settings.log_level, settings.log_json)
    get_storage()
    logger.info("startup", extra={"provider": settings.llm_provider, "env": settings.environment})
    yield
    await async_engine.dispose()


app = FastAPI(title=settings.app_name, version="1.0.0", description=DESCRIPTION, lifespan=lifespan)
register_exception_handlers(app)


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    token = request_id_var.set(rid)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    elapsed = (time.perf_counter() - start) * 1000
    response.headers["X-Request-ID"] = rid
    response.headers["Server-Timing"] = f"app;dur={elapsed:.1f}"
    if not request.url.path.endswith(("/health/live", "/health/ready")):
        logger.info(
            "request",
            extra={
                "request_id": rid,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "ms": round(elapsed, 1),
            },
        )
    return response


api = APIRouter(prefix="/api/v1")
api.include_router(documents.router)
api.include_router(chat.router)
api.include_router(metrics.router)
api.include_router(health.router)
app.include_router(api)

_STATIC = Path(__file__).resolve().parent / "static"
_SAMPLES = Path(__file__).resolve().parents[1] / "samples"


@app.get("/", include_in_schema=False)
async def ui():
    return FileResponse(_STATIC / "index.html")


# More specific mount first: /static would otherwise swallow /static/samples.
if _SAMPLES.is_dir():
    app.mount("/static/samples", StaticFiles(directory=_SAMPLES), name="samples")
app.mount("/static", StaticFiles(directory=_STATIC), name="static")
