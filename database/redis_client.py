import os
import json
import logging
from typing import Any, Optional
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

class RedisClient:
    """Manages async Redis connection for caching and deduplication."""
    
    def __init__(self, url: str = REDIS_URL) -> None:
        self.url = url
        self.client: Optional[Redis] = None

    async def connect(self) -> None:
        if self.client is None:
            self.client = Redis.from_url(self.url, decode_responses=True)
            logger.info("Connected to Redis at %s", self.url)

    async def disconnect(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None
            logger.info("Disconnected from Redis")

    # ------------------------------------------------------------------
    # Basic Key/Value caching (JSON serialized)
    # ------------------------------------------------------------------
    async def set_json(self, key: str, value: Any, ttl_seconds: int = 0) -> None:
        if not self.client:
            return
        payload = json.dumps(value)
        if ttl_seconds > 0:
            await self.client.setex(key, ttl_seconds, payload)
        else:
            await self.client.set(key, payload)

    async def get_json(self, key: str) -> Any:
        if not self.client:
            return None
        payload = await self.client.get(key)
        if payload:
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                pass
        return None

    # ------------------------------------------------------------------
    # Sets (for Deduplication & Fast Lookups)
    # ------------------------------------------------------------------
    async def set_add(self, key: str, member: str) -> bool:
        """Returns True if member was added (didn't exist)."""
        if not self.client:
            return False
        added = await self.client.sadd(key, member)
        return bool(added)
        
    async def set_members(self, key: str) -> set[str]:
        if not self.client:
            return set()
        return set(await self.client.smembers(key))

redis_db = RedisClient()
