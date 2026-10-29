"""Test setup: real PostgreSQL+pgvector (separate `vault_test` DB), Redis db 15,
offline LLM provider, and the Celery hop replaced by an inline pipeline run."""

import os
import tempfile

TEST_DB = "vault_test"
_base = os.environ.get("TEST_DATABASE_HOST", "localhost:55432")
os.environ.update(
    ENVIRONMENT="test",
    DATABASE_URL=f"postgresql+asyncpg://vault:vault@{_base}/{TEST_DB}",
    REDIS_URL=os.environ.get("TEST_REDIS_URL", "redis://localhost:56379/15"),
    LLM_PROVIDER="fake",
    STORAGE_DIR=tempfile.mkdtemp(prefix="vault-test-"),
    API_KEYS="test-key:alice,other-key:bob",
    RATE_LIMIT_ENABLED="false",
    LOG_JSON="false",
    LOG_LEVEL="WARNING",
)

import psycopg  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db.session import async_engine  # noqa: E402
from app.services.cache import get_sync_redis  # noqa: E402

ALICE = {"X-API-Key": "test-key"}
BOB = {"X-API-Key": "other-key"}


def _ensure_database() -> None:
    with psycopg.connect(f"postgresql://vault:vault@{_base}/vault", autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone():
            conn.execute(f'CREATE DATABASE "{TEST_DB}"')


@pytest.fixture(scope="session", autouse=True)
def _database():
    _ensure_database()
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "base") if _has_schema() else None
    command.upgrade(cfg, "head")
    yield


def _has_schema() -> bool:
    with psycopg.connect(f"postgresql://vault:vault@{_base}/{TEST_DB}") as conn:
        return bool(conn.execute("SELECT to_regclass('public.alembic_version')").fetchone()[0])


@pytest.fixture(autouse=True)
async def _clean():
    async with async_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE llm_usage, processing_events, chat_messages, chat_sessions, document_chunks, documents "
                "RESTART IDENTITY CASCADE"
            )
        )
    get_sync_redis().flushdb()
    yield


@pytest.fixture(autouse=True)
def inline_worker(monkeypatch):
    """Run the pipeline synchronously instead of sending to Celery."""
    from app.workers import pipeline

    calls: list = []

    def run(document_id):
        calls.append(document_id)
        if pipeline.ingest(document_id):
            pipeline.analyze(document_id)
        return True

    monkeypatch.setattr("app.services.ingestion.enqueue_ingest", run)
    monkeypatch.setattr("app.api.routes.documents.enqueue_ingest", run)
    return calls


@pytest.fixture
async def client():
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test/api/v1") as c:
        yield c


CONTRACT = (
    "# Master Services Agreement\n\n"
    "This agreement is between Acme Corp and Globex Consulting.\n\n"
    "## Termination\nEither party may terminate this agreement with 30 days written notice.\n\n"
    "## Payment\nInvoices are due within 45 days of receipt. Late payments accrue 1.5 percent interest per month.\n"
)
HANDBOOK = (
    "Employee Handbook\n\n"
    "Full-time employees receive 25 days of paid annual leave per calendar year.\n\n"
    "Primary caregivers receive 16 weeks of fully paid parental leave.\n"
)


async def upload(client, name: str, content: str | bytes, headers=ALICE):
    data = content.encode() if isinstance(content, str) else content
    return await client.post("/documents", files={"file": (name, data)}, headers=headers)
