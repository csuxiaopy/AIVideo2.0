from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class TaskEnvelope:
    camera_id: str
    priority: str
    task_id: str
    stream: str | None = None
    message_id: str | None = None
    created_at: datetime | None = None


class AnalysisQueue:
    def __init__(self, redis_url: str, maxsize: int = 4096, prefix: str = "monitor:tasks"):
        self.redis_url = redis_url
        self.prefix = prefix
        self.maxsize = maxsize
        self.streams = {
            "critical": f"{prefix}:critical",
            "high": f"{prefix}:high",
            "normal": f"{prefix}:normal",
            "low": f"{prefix}:low",
        }
        self.fallback: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=maxsize)
        self.redis = None
        self.redis_available = False
        self.consumer = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.group = f"{prefix.replace(':', '-')}-workers"
        self.sequence = 0
        self.recovered: asyncio.Queue[TaskEnvelope] = asyncio.Queue(maxsize=maxsize)
        self.last_reclaim_at = 0.0
        self.backend = "in_memory"
        self.degraded_since: str | None = None
        self.last_error: str | None = None
        self.reconnect_attempts = 0
        self.last_recovered_at: str | None = None
        self.rejected_total = 0
        self.last_rejected_at: str | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._closed = False
        self._state_lock = asyncio.Lock()

    async def _new_client(self):
        import redis.asyncio as redis

        client = redis.from_url(self.redis_url, decode_responses=True)
        await client.ping()
        for stream in self.streams.values():
            try:
                await client.xgroup_create(stream, self.group, id="0", mkstream=True)
            except Exception as exc:
                if "BUSYGROUP" not in str(exc):
                    await client.aclose()
                    raise
        return client

    def _degrade(self, exc: Exception) -> None:
        self.redis_available = False
        self.backend = "in_memory"
        self.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
        if self.degraded_since is None:
            self.degraded_since = datetime.now(timezone.utc).isoformat()

    async def _mark_unavailable(self, exc: Exception) -> None:
        client, self.redis = self.redis, None
        self._degrade(exc)
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    async def _migrate_fallback(self, client) -> None:
        async with self._state_lock:
            self.backend = "recovering"
            while True:
                try:
                    _, _, envelope = self.fallback.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    await client.xadd(self.streams[envelope.priority], {
                        "task_id": envelope.task_id,
                        "camera_id": envelope.camera_id,
                        "priority": envelope.priority,
                        "created_at": (envelope.created_at or datetime.now(timezone.utc)).isoformat(),
                    })
                except Exception:
                    self.sequence += 1
                    ranks = {"critical": 0, "high": 1, "normal": 2, "low": 3}
                    self.fallback.put_nowait((ranks[envelope.priority], self.sequence, envelope))
                    self.fallback.task_done()
                    raise
                self.fallback.task_done()
            self.redis = client
            self.redis_available = True
            self.backend = "redis"
            self.degraded_since = None
            self.last_error = None
            self.last_recovered_at = datetime.now(timezone.utc).isoformat()

    async def _reconnect_loop(self) -> None:
        delay = 5
        while not self._closed:
            if self.redis_available:
                await asyncio.sleep(5)
                continue
            client = None
            try:
                self.reconnect_attempts += 1
                client = await self._new_client()
                await self._migrate_fallback(client)
                delay = 5
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if client is not None and client is not self.redis:
                    try:
                        await client.aclose()
                    except Exception:
                        pass
                self._degrade(exc)
                await asyncio.sleep(delay)
                delay = min(30, delay * 2)

    async def start(self) -> None:
        try:
            client = await self._new_client()
            self.redis = client
            self.redis_available = True
            self.backend = "redis"
        except Exception as exc:
            self._degrade(exc)
        self._reconnect_task = asyncio.create_task(
            self._reconnect_loop(), name=f"{self.prefix}-redis-reconnect"
        )

    async def close(self) -> None:
        self._closed = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
            await asyncio.gather(self._reconnect_task, return_exceptions=True)
        if self.redis is not None:
            await self.redis.aclose()

    async def enqueue(self, camera_id: str, priority: str = "normal") -> str | None:
        priority = priority if priority in self.streams else "normal"
        task_id = uuid.uuid4().hex
        if self.redis_available and self.redis is not None:
            try:
                redis_depths = await self.depths()
                if self.redis_available and self.redis is not None and sum(redis_depths.values()) >= self.maxsize:
                    self._record_rejection()
                    return None
                if self.redis_available and self.redis is not None:
                    stream = self.streams[priority]
                    return await self.redis.xadd(
                        stream,
                        {"task_id": task_id, "camera_id": camera_id, "priority": priority, "created_at": datetime.now(timezone.utc).isoformat()},
                    )
            except Exception as exc:
                await self._mark_unavailable(exc)
        async with self._state_lock:
            if self.redis_available and self.redis is not None:
                try:
                    return await self.redis.xadd(self.streams[priority], {
                        "task_id": task_id, "camera_id": camera_id, "priority": priority,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    })
                except Exception as exc:
                    await self._mark_unavailable(exc)
            ranks = {"critical": 0, "high": 1, "normal": 2, "low": 3}
            self.sequence += 1
            try:
                self.fallback.put_nowait((ranks[priority], self.sequence, TaskEnvelope(
                    camera_id, priority, task_id, created_at=datetime.now(timezone.utc)
                )))
                return task_id
            except asyncio.QueueFull:
                self._record_rejection()
                return None

    def _record_rejection(self) -> None:
        self.rejected_total += 1
        self.last_rejected_at = datetime.now(timezone.utc).isoformat()

    async def get(self) -> TaskEnvelope:
        while True:
            if not self.redis_available or self.redis is None:
                try:
                    _, _, envelope = await asyncio.wait_for(self.fallback.get(), timeout=1)
                    return envelope
                except asyncio.TimeoutError:
                    continue
            streams = {stream: ">" for stream in self.streams.values()}
            try:
                try:
                    return self.recovered.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                await self._reclaim_stale()
                try:
                    return self.recovered.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                messages = await self.redis.xreadgroup(
                    self.group, self.consumer, streams=streams, count=1, block=1000
                )
                if messages:
                    for extra_stream, extra_rows in messages[1:]:
                        for extra_id, extra_fields in extra_rows:
                            self.recovered.put_nowait(TaskEnvelope(
                                camera_id=extra_fields["camera_id"],
                                priority=extra_fields.get("priority", "normal"),
                                task_id=extra_fields.get("task_id", extra_id),
                                stream=extra_stream, message_id=extra_id,
                                created_at=datetime.fromisoformat(extra_fields["created_at"]) if extra_fields.get("created_at") else None,
                            ))
                    stream, rows = messages[0]
                    message_id, fields = rows[0]
                    return TaskEnvelope(
                        camera_id=fields["camera_id"], priority=fields.get("priority", "normal"),
                        task_id=fields.get("task_id", message_id), stream=stream, message_id=message_id,
                        created_at=datetime.fromisoformat(fields["created_at"]) if fields.get("created_at") else None,
                    )
            except Exception as exc:
                await self._mark_unavailable(exc)

    async def _reclaim_stale(self) -> None:
        """Recover work abandoned by a dead worker without stealing normal long VLM calls."""
        now = time.monotonic()
        if now - self.last_reclaim_at < 30 or self.redis is None:
            return
        self.last_reclaim_at = now
        for stream in self.streams.values():
            start_id = "0-0"
            while not self.recovered.full():
                claimed = await self.redis.xautoclaim(
                    stream, self.group, self.consumer,
                    min_idle_time=600_000, start_id=start_id, count=100,
                )
                start_id, rows = claimed[0], claimed[1]
                for message_id, fields in rows:
                    self.recovered.put_nowait(TaskEnvelope(
                        camera_id=fields["camera_id"], priority=fields.get("priority", "normal"),
                        task_id=fields.get("task_id", message_id), stream=stream,
                        message_id=message_id,
                        created_at=datetime.fromisoformat(fields["created_at"]) if fields.get("created_at") else None,
                    ))
                if not rows or start_id == "0-0":
                    break

    async def ack(self, task: TaskEnvelope) -> None:
        if task.stream and task.message_id:
            if not self.redis_available or self.redis is None:
                raise RuntimeError("Redis unavailable; pending message remains unacknowledged")
            try:
                await self.redis.xack(task.stream, self.group, task.message_id)
                await self.redis.xdel(task.stream, task.message_id)
            except Exception as exc:
                await self._mark_unavailable(exc)
                raise
        else:
            self.fallback.task_done()

    async def retry(self, task: TaskEnvelope, delay_seconds: float = 0.05) -> bool:
        """Requeue transiently blocked work without losing its original age."""
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        if self.redis_available and self.redis is not None:
            try:
                redis_depths = await self.depths()
                if self.redis_available and self.redis is not None and sum(redis_depths.values()) >= self.maxsize:
                    self._record_rejection()
                    return False
                if self.redis_available and self.redis is not None:
                    stream = self.streams.get(task.priority, self.streams["normal"])
                    await self.redis.xadd(stream, {
                        "task_id": task.task_id, "camera_id": task.camera_id,
                        "priority": task.priority,
                        "created_at": (task.created_at or datetime.now(timezone.utc)).isoformat(),
                    })
                    return True
            except Exception as exc:
                await self._mark_unavailable(exc)
        async with self._state_lock:
            if self.redis_available and self.redis is not None:
                try:
                    await self.redis.xadd(self.streams.get(task.priority, self.streams["normal"]), {
                        "task_id": task.task_id, "camera_id": task.camera_id,
                        "priority": task.priority,
                        "created_at": (task.created_at or datetime.now(timezone.utc)).isoformat(),
                    })
                    return True
                except Exception as exc:
                    await self._mark_unavailable(exc)
            ranks = {"critical": 0, "high": 1, "normal": 2, "low": 3}
            self.sequence += 1
            try:
                self.fallback.put_nowait((
                    ranks.get(task.priority, ranks["normal"]), self.sequence,
                    TaskEnvelope(task.camera_id, task.priority, task.task_id,
                                 created_at=task.created_at),
                ))
                return True
            except asyncio.QueueFull:
                self._record_rejection()
                return False

    async def acquire_camera(self, camera_id: str, ttl_seconds: int = 120) -> bool:
        if self.redis_available and self.redis is not None:
            try:
                return bool(await self.redis.set(
                    f"{self.prefix}:camera-lock:{camera_id}",
                    self.consumer,
                    nx=True,
                    ex=ttl_seconds,
                ))
            except Exception as exc:
                await self._mark_unavailable(exc)
        return True

    async def release_camera(self, camera_id: str) -> None:
        if self.redis_available and self.redis is not None:
            try:
                key = f"{self.prefix}:camera-lock:{camera_id}"
                value = await self.redis.get(key)
                if value == self.consumer:
                    await self.redis.delete(key)
            except Exception as exc:
                await self._mark_unavailable(exc)
                raise

    async def depths(self) -> dict[str, int]:
        if self.redis_available and self.redis is not None:
            try:
                depths: dict[str, int] = {}
                for priority, stream in self.streams.items():
                    groups = await self.redis.xinfo_groups(stream)
                    group = next((item for item in groups if item.get("name") == self.group), {})
                    depths[priority] = int(group.get("pending", 0)) + int(group.get("lag") or 0)
                return depths
            except Exception as exc:
                await self._mark_unavailable(exc)
        counts = {priority: 0 for priority in self.streams}
        for _, _, envelope in list(self.fallback._queue):
            counts[envelope.priority] += 1
        return counts

    def status(self) -> dict[str, Any]:
        rejected_recently = False
        if self.last_rejected_at:
            rejected_recently = (
                datetime.now(timezone.utc) - datetime.fromisoformat(self.last_rejected_at)
            ).total_seconds() < 300
        return {
            "backend": self.backend,
            "redis_connected": self.redis_available,
            "degraded_since": self.degraded_since,
            "last_error": self.last_error,
            "reconnect_attempts": self.reconnect_attempts,
            "last_recovered_at": self.last_recovered_at,
            "fallback_depth": self.fallback.qsize(),
            "capacity": self.maxsize,
            "rejected_total": self.rejected_total,
            "last_rejected_at": self.last_rejected_at,
            "rejected_recently": rejected_recently,
        }
