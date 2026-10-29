from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

# API: async engine (asyncpg). Worker: sync engine (psycopg). Celery tasks are synchronous.
async_engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
)
AsyncSessionLocal = async_sessionmaker(async_engine, expire_on_commit=False)

_sync_engine = None
_SyncSessionLocal: sessionmaker[Session] | None = None


def get_sync_sessionmaker() -> sessionmaker[Session]:
    global _sync_engine, _SyncSessionLocal
    if _SyncSessionLocal is None:
        _sync_engine = create_engine(settings.sync_database_url, pool_size=5, pool_pre_ping=True)
        _SyncSessionLocal = sessionmaker(_sync_engine, expire_on_commit=False)
    return _SyncSessionLocal


@contextmanager
def sync_session() -> Iterator[Session]:
    session = get_sync_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def get_db() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
