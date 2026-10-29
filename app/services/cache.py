"""Redis helpers. All cache operations fail open: a Redis outage degrades
performance (cache misses) but never breaks a request."""

import hashlib
import json
import logging
from functools import lru_cache
from typing import Any

import redis
import redis.asyncio as aredis

from app.core.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_async_redis() -> aredis.Redis:
    return aredis.from_url(settings.redis_url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)


@lru_cache(maxsize=1)
def get_sync_redis() -> redis.Redis:
    return redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)


def cache_key(namespace: str, *parts: Any) -> str:
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:32]
    return f"vault:{namespace}:{digest}"


async def aget_json(key: str) -> Any | None:
    try:
        raw = await get_async_redis().get(key)
        return json.loads(raw) if raw else None
    except (redis.RedisError, OSError) as exc:
        logger.warning("cache get failed", extra={"key": key, "error": str(exc)})
        return None


async def aset_json(key: str, value: Any, ttl: int | None = None) -> None:
    try:
        await get_async_redis().set(key, json.dumps(value), ex=ttl or settings.cache_ttl_s)
    except (redis.RedisError, OSError) as exc:
        logger.warning("cache set failed", extra={"key": key, "error": str(exc)})
