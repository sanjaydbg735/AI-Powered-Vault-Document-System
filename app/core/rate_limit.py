"""Fixed-window rate limiter in Redis (works across API replicas). Fails open."""

import logging
import time

import redis
from fastapi import Depends, Response

from app.api.deps import get_owner
from app.core.config import settings
from app.core.errors import RateLimitedError
from app.services.cache import get_async_redis

logger = logging.getLogger(__name__)


def rate_limit(bucket: str, limit_attr: str):
    async def dependency(response: Response, owner_id: str = Depends(get_owner)) -> None:
        if not settings.rate_limit_enabled:
            return
        limit = getattr(settings, limit_attr)
        window = int(time.time() // 60)
        key = f"vault:rl:{bucket}:{owner_id}:{window}"
        try:
            pipe = get_async_redis().pipeline()
            pipe.incr(key)
            pipe.expire(key, 61)
            count, _ = await pipe.execute()
        except (redis.RedisError, OSError) as exc:
            logger.warning("rate limiter unavailable, allowing request", extra={"error": str(exc)})
            return
        remaining = max(0, limit - count)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        if count > limit:
            retry_after = 60 - int(time.time()) % 60
            raise RateLimitedError(f"Rate limit of {limit}/min exceeded for {bucket}", retry_after=retry_after)

    return Depends(dependency)


chat_limit = rate_limit("chat", "rate_limit_chat_per_min")
upload_limit = rate_limit("upload", "rate_limit_upload_per_min")
ai_limit = rate_limit("ai", "rate_limit_ai_per_min")
