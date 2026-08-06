from __future__ import annotations

import asyncio
import json
import os
import socket
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


class AnalysisQueue:
    def __init__(self, redis_url: str, maxsize: int = 4096, prefix: str = "monitor:tasks"):
        self.redis_url = redis_url
        self.prefix = prefix
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

    async def start(self) -> None:
        try:
            import redis.asyncio as redis

            client = redis.from_url(self.redis_url, decode_responses=True)
            await client.ping()
            for stream in self.streams.values():
                try:
                    await client.xgroup_create(stream, self.group, id="0", mkstream=True)
                except Exception as exc:
                    if "BUSYGROUP" not in str(exc):
                        raise
            self.redis = client
            self.redis_available = True
        except Exception:
            self.redis = None
            self.redis_available = False

    async def close(self) -> None:
        if self.redis is not None:
            await self.redis.aclose()

    async def enqueue(self, camera_id: str, priority: str = "normal") -> str | None:
        priority = priority if priority in self.streams else "normal"
        task_id = uuid.uuid4().hex
        if self.redis_available and self.redis is not None:
            stream = self.streams[priority]
            return await self.redis.xadd(
                stream,
                {"task_id": task_id, "camera_id": camera_id, "priority": priority, "created_at": datetime.now(timezone.utc).isoformat()},
                maxlen=10000,
                approximate=True,
            )
        ranks = {"critical": 0, "high": 1, "normal": 2, "low": 3}
        self.sequence += 1
        try:
            self.fallback.put_nowait((ranks[priority], self.sequence, TaskEnvelope(camera_id, priority, task_id)))
            return task_id
        except asyncio.QueueFull:
            return None

    async def get(self) -> TaskEnvelope:
        if self.redis_available and self.redis is not None:
            streams = {stream: ">" for stream in self.streams.values()}
            while True:
                messages = await self.redis.xreadgroup(
                    self.group, self.consumer, streams=streams, count=1, block=1000
                )
                if messages:
                    stream, rows = messages[0]
                    message_id, fields = rows[0]
                    return TaskEnvelope(
                        camera_id=fields["camera_id"], priority=fields.get("priority", "normal"),
                        task_id=fields.get("task_id", message_id), stream=stream, message_id=message_id,
                    )
        _, _, envelope = await self.fallback.get()
        return envelope

    async def ack(self, task: TaskEnvelope) -> None:
        if self.redis_available and self.redis is not None and task.stream and task.message_id:
            await self.redis.xack(task.stream, self.group, task.message_id)
        else:
            self.fallback.task_done()

    async def acquire_camera(self, camera_id: str, ttl_seconds: int = 120) -> bool:
        if self.redis_available and self.redis is not None:
            return bool(await self.redis.set(f"monitor:camera-lock:{camera_id}", self.consumer, nx=True, ex=ttl_seconds))
        return True

    async def release_camera(self, camera_id: str) -> None:
        if self.redis_available and self.redis is not None:
            key = f"monitor:camera-lock:{camera_id}"
            value = await self.redis.get(key)
            if value == self.consumer:
                await self.redis.delete(key)

    async def depths(self) -> dict[str, int]:
        if self.redis_available and self.redis is not None:
            depths: dict[str, int] = {}
            for priority, stream in self.streams.items():
                groups = await self.redis.xinfo_groups(stream)
                group = next((item for item in groups if item.get("name") == self.group), {})
                depths[priority] = int(group.get("pending", 0)) + int(group.get("lag") or 0)
            return depths
        counts = {priority: 0 for priority in self.streams}
        for _, _, envelope in list(self.fallback._queue):
            counts[envelope.priority] += 1
        return counts
