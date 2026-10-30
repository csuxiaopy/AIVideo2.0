"""Short-lived summary cache, shared across Web workers when Redis is available."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
import threading
import time

from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry


class DashboardCache:
    def __init__(self, ttl=5, client=None, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._lock = threading.Lock()
        self._key, self._value, self._expires = None, None, 0.0
        self._redis_retry = 0.0
        self.client = client
        if self.client is None and os.environ.get("REDIS_URL"):
            self.client = Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=.2,
                                         socket_timeout=.2, retry=Retry(NoBackoff(), 0))
        namespace = hashlib.sha256(os.environ.get("DATABASE_URL", "local").encode()).hexdigest()[:16]
        self.prefix = f"aivideo:dashboard:v2:{namespace}:"

    def get(self, day, loader):
        key = self.prefix + day
        with self._lock:
            if key == self._key and self.clock() < self._expires:
                return deepcopy(self._value)
            distributed_lock = None
            acquired = False
            client = self.client if self.clock() >= self._redis_retry else None
            try:
                if client is not None:
                    # Fetch TTL with the value, so local hits never extend Redis
                    # freshness by another complete cache interval.
                    def shared():
                        pipe = client.pipeline(transaction=False)
                        value, remaining_ms = pipe.get(key).pttl(key).execute()
                        if value is not None and remaining_ms > 0:
                            result = json.loads(value)
                            self._remember(key, result, min(self.ttl, remaining_ms / 1000))
                            return deepcopy(result)
                        return None
                    try:
                        value = shared()
                        if value is not None:
                            return value
                        distributed_lock = client.lock(key + ":refresh", timeout=10, blocking_timeout=.5)
                        acquired = distributed_lock.acquire()
                        # The prior owner may have populated the cache while we
                        # waited, including when acquisition timed out.
                        value = shared()
                        if value is not None:
                            return value
                    except (RedisError, ValueError, TypeError):
                        self._redis_retry = self.clock() + 30
                        client = None
                result = loader()
                if client is not None and acquired:
                    try:
                        client.setex(key, self.ttl, json.dumps(result, separators=(",", ":")))
                    except RedisError:
                        self._redis_retry = self.clock() + 30
                self._remember(key, result, self.ttl)
                return deepcopy(result)
            finally:
                if acquired:
                    try:
                        distributed_lock.release()
                    except RedisError:
                        pass

    def _remember(self, key, result, ttl):
        self._key, self._value, self._expires = key, deepcopy(result), self.clock() + ttl
