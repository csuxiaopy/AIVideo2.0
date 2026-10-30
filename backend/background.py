"""Bounded asynchronous primitives for the realtime pipeline.

The event loop owns admission and mutable queue state. Blocking handlers receive
immutable JSON and open their own database sessions in a dedicated I/O pool.
Submission is not a durability acknowledgement: a job becomes durable only after
the outbox INSERT commits. Callers must retain rejected work and stop advancing
the affected event. No unbounded create_task-per-frame pattern is used here.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

logger = logging.getLogger(__name__)


class ReviewDeferred(RuntimeError):
    """Temporary upstream outage; retain immutable evidence and release worker."""

    retry_after = 3.0


@dataclass(frozen=True)
class Job:
    id: str
    key: str
    kind: str
    camera_id: str | None
    payload_json: str
    evidence_ref: str | None = None

    @classmethod
    def make(cls, kind: str, key: str, payload: dict, camera_id: str | None = None,
             evidence_ref: str | None = None) -> "Job":
        if not kind or len(kind) > 40 or not key or len(key) > 240:
            raise ValueError("Invalid background job kind/idempotency key")
        return cls(str(uuid4()), key, kind, camera_id,
                   json.dumps(payload, ensure_ascii=False, allow_nan=False), evidence_ref)


class IdempotencyConflict(ValueError):
    """A durable key already exists with different immutable work."""

    def __init__(self, job: Job, existing):
        self.job_id = job.id
        self.key = job.key
        self.kind = job.kind
        self.camera_id = job.camera_id
        self.existing_id = existing.id
        self.existing_status = existing.status
        super().__init__("Idempotency key reused with different payload")


class IOPool:
    """Dedicated bounded submission pool, never shared with GPU detect calls."""

    def __init__(self, workers: int = 4):
        if workers < 1:
            raise ValueError("workers must be positive")
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="monitor-io")
        self.slots = asyncio.Semaphore(workers)
        self.closed = False

    async def run(self, fn: Callable, *args, **kwargs):
        if self.closed:
            raise RuntimeError("I/O pool is closed")
        async with self.slots:
            future = asyncio.get_running_loop().run_in_executor(self.executor, partial(fn, *args, **kwargs))
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                # Cancellation cannot stop a running thread. Keep its slot until
                # it exits, preventing a cancellation storm from filling the pool.
                await future
                raise

    async def close(self):
        self.closed = True
        await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)


class CriticalIngress:
    """Bounded FIFO; head is retained on persistence failure, not acknowledged."""

    def __init__(self, io: IOPool, persist: Callable[[Job], str], capacity: int = 4096):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.io, self.persist, self.capacity = io, persist, capacity
        self.persist_batch = None
        self.pending: OrderedDict[str, tuple[Job, float]] = OrderedDict()
        self.failures = 0
        self.consecutive_failures = 0
        self.last_error: str | None = None
        self.persisted = 0
        self.last_success_at: str | None = None
        self.idempotency_conflicts = 0
        self.isolated = 0
        self.dead_letters: OrderedDict[str, dict] = OrderedDict()
        self.closed = False
        self.task: asyncio.Task | None = None

    def submit(self, job: Job) -> bool:
        if self.closed:
            return False
        if job.key in self.pending:
            if self.pending[job.key][0] != job:
                previous = self.pending[job.key][0]
                if (previous.kind, previous.camera_id, previous.payload_json, previous.evidence_ref) != (
                    job.kind, job.camera_id, job.payload_json, job.evidence_ref
                ):
                    raise ValueError("Idempotency key reused with different work")
            return True
        if len(self.pending) >= self.capacity:
            return False
        self.pending[job.key] = job, time.monotonic()
        return True

    async def flush_one(self) -> bool:
        if not self.pending:
            return False
        batch = list(self.pending.items())[:100 if self.persist_batch else 1]
        try:
            if self.persist_batch:
                await self.io.run(self.persist_batch, [item[0] for _, item in batch])
            else:
                await self.io.run(self.persist, batch[0][1][0])
        except IdempotencyConflict as exc:
            if self.persist_batch and len(batch) > 1:
                return await self._flush_individually(batch, exc)
            # A durable row is already the authoritative audit record. Isolate
            # the conflicting reconstruction so it cannot poison unrelated
            # analysis/model-log writes after a restart.
            self._isolate_conflict(exc)
            return True
        except Exception as exc:
            if self.persist_batch and len(batch) > 1:
                return await self._flush_individually(batch, exc)
            self.failures += 1
            self.consecutive_failures += 1
            # Expose type, not exception text which may contain credentials/SQL.
            self.last_error = type(exc).__name__
            return False
        for key, _ in batch:
            self.pending.pop(key)
        self.persisted += len(batch)
        self.consecutive_failures = 0
        self.last_error = None
        self.last_success_at = datetime.now(timezone.utc).isoformat()
        return True

    async def _flush_individually(self, batch, _batch_error) -> bool:
        """Salvage valid jobs from a failed batch and isolate known conflicts."""
        progressed = False
        remaining_error = None
        for key, (job, _) in batch:
            try:
                await self.io.run(self.persist, job)
            except IdempotencyConflict as exc:
                self._isolate_conflict(exc)
                progressed = True
            except Exception as exc:
                remaining_error = exc
                # Keep unpersisted work, but move it behind healthy jobs so one
                # record cannot cause head-of-line blocking.
                if key in self.pending:
                    self.pending.move_to_end(key)
            else:
                self.pending.pop(key, None)
                self.persisted += 1
                progressed = True
        if progressed:
            self.last_success_at = datetime.now(timezone.utc).isoformat()
        if remaining_error is not None:
            self.failures += 1
            self.consecutive_failures += 1
            self.last_error = type(remaining_error).__name__
        else:
            self.consecutive_failures = 0
            self.last_error = None
        return progressed

    def _isolate_conflict(self, exc: IdempotencyConflict) -> None:
        self.pending.pop(exc.key, None)
        self.failures += 1
        self.idempotency_conflicts += 1
        self.isolated += 1
        key_hash = hashlib.sha256(exc.key.encode()).hexdigest()[:16]
        self.dead_letters[exc.job_id] = {
            "job_id": exc.job_id, "kind": exc.kind,
            "camera_id": exc.camera_id, "key_hash": key_hash,
            "conflict": "payload_mismatch", "existing_id": exc.existing_id,
            "existing_status": exc.existing_status,
        }
        while len(self.dead_letters) > 100:
            self.dead_letters.popitem(last=False)
        logger.error(
            "Isolated idempotency conflict job_id=%s kind=%s camera_id=%s key_hash=%s existing_id=%s existing_status=%s",
            exc.job_id, exc.kind, exc.camera_id, key_hash,
            exc.existing_id, exc.existing_status,
        )
        self.consecutive_failures = 0
        self.last_error = None

    def start(self):
        if not self.task:
            self.task = asyncio.create_task(self._run(), name="critical-outbox-writer")

    async def _run(self):
        while not self.closed:
            if not await self.flush_one():
                await asyncio.sleep(1 if self.last_error else 0.05)

    def status(self):
        oldest = next(iter(self.pending.values()), None)
        return {"depth": len(self.pending), "capacity": self.capacity,
                "utilization": round(len(self.pending) / self.capacity, 4),
                "oldest_age_seconds": time.monotonic() - oldest[1] if oldest else 0,
                "failures": self.failures, "error": self.last_error,
                "consecutive_failures": self.consecutive_failures,
                "last_success_at": self.last_success_at,
                "idempotency_conflicts": self.idempotency_conflicts,
                "isolated": self.isolated, "dead_letters": len(self.dead_letters),
                "degraded": bool(self.last_error or self.dead_letters
                                 or len(self.pending) >= self.capacity),
                "persisted": self.persisted}

    async def close(self) -> int:
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        # A nonzero return MUST be reported as unpersisted shutdown work.
        return len(self.pending)


class LatestWriter:
    """Coalesce replaceable status only; never use for event/count increments."""

    def __init__(self, io: IOPool, write_batch: Callable, max_keys: int,
                 batch_size: int = 100, interval: float = 1.0):
        if min(max_keys, batch_size) < 1 or interval <= 0:
            raise ValueError("Invalid latest writer bounds")
        self.io, self.write_batch = io, write_batch
        self.max_keys, self.batch_size, self.interval = max_keys, batch_size, interval
        self.pending: OrderedDict[str, tuple[int, str]] = OrderedDict()
        self.version = 0
        self.failures = 0
        self.error: str | None = None
        self.task: asyncio.Task | None = None

    def submit(self, key: str, values: dict) -> bool:
        if key not in self.pending and len(self.pending) >= self.max_keys:
            return False
        self.version += 1
        self.pending[key] = self.version, json.dumps(values, allow_nan=False)
        return True

    async def flush(self):
        batch = list(self.pending.items())[:self.batch_size]
        if not batch:
            return
        try:
            await self.io.run(self.write_batch, [(key, json.loads(item[1])) for key, item in batch])
        except Exception as exc:
            self.failures += 1
            self.error = type(exc).__name__
            return
        for key, item in batch:
            if self.pending.get(key) == item:
                del self.pending[key]
            elif key in self.pending:
                # Fairness: a continuously updated camera cannot starve others.
                self.pending.move_to_end(key)
        self.error = None

    def start(self):
        if not self.task:
            self.task = asyncio.create_task(self._run(), name="latest-status-writer")

    async def _run(self):
        while True:
            await asyncio.sleep(self.interval)
            await self.flush()

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


@dataclass(frozen=True)
class ReviewRequest:
    camera_id: str
    kind: str
    event_id: str
    config_version: int
    sampled_at: str
    evidence_ref: str
    payload_json: str = "{}"


@dataclass(frozen=True)
class ReviewValidity:
    """Current event-loop snapshot, not a mutable rule state/ORM reference."""

    event_id: str
    config_version: int
    enabled: bool
    online: bool
    scheduled: bool
    event_active: bool
    latest_frame_at: datetime | None
    occupied: bool = False

    def rejection_reason(self, request: ReviewRequest, now: datetime,
                         max_frame_age: float, max_evidence_age: float) -> str | None:
        if request.config_version != self.config_version:
            return "configuration_changed"
        if request.event_id != self.event_id or not self.event_active:
            return "event_ended"
        if not self.enabled or not self.online:
            return "camera_unavailable"
        if not self.scheduled:
            return "schedule_ended"
        if request.kind == "off_duty" and self.occupied:
            return "person_returned"
        try:
            sampled = datetime.fromisoformat(request.sampled_at)
            if sampled.tzinfo is None or now.tzinfo is None:
                return "invalid_timestamp"
            age = (now - sampled).total_seconds()
            if age < 0 or age > max_evidence_age:
                return "evidence_expired"
            if self.latest_frame_at is None or self.latest_frame_at.tzinfo is None:
                return "frame_unavailable"
            frame_age = (now - self.latest_frame_at).total_seconds()
            if frame_age < 0 or frame_age > max_frame_age:
                return "frame_expired"
        except (ValueError, TypeError):
            return "invalid_timestamp"
        return None


class ReviewQueue:
    """One active and one latest pending request per camera/review kind.

    handler and completion run on the owning loop; a completion must validate the
    request against current state before applying it. Handler errors also reach
    completion, never becoming implicit confirmation.
    """

    def __init__(self, handler: Callable, completion: Callable, concurrency: int = 2,
                 max_keys: int = 256, timeout: float = 60):
        if concurrency < 1 or max_keys < 1 or timeout <= 0:
            raise ValueError("Invalid review queue bounds")
        self.handler, self.completion = handler, completion
        self.concurrency, self.max_keys, self.timeout = concurrency, max_keys, timeout
        self.pending: OrderedDict[tuple[str, str], tuple[ReviewRequest, float]] = OrderedDict()
        self.active: dict[tuple[str, str], ReviewRequest] = {}
        self.unapplied: dict[tuple[str, str], tuple[ReviewRequest, Any, Exception | None]] = {}
        self.deferred: dict[tuple[str, str], tuple[ReviewRequest, float, float]] = {}
        self.tasks: list[asyncio.Task] = []
        self.failures = 0
        self.closed = False

    def submit(self, request: ReviewRequest) -> bool:
        if self.closed:
            return False
        key = request.camera_id, request.kind
        if key in self.deferred:
            return False
        if key not in self.pending and key not in self.active and key not in self.unapplied and len(set(self.pending) | set(self.active) | set(self.unapplied) | set(self.deferred)) >= self.max_keys:
            return False
        if self.active.get(key) == request:
            return True
        age = self.pending[key][1] if key in self.pending else time.monotonic()
        self.pending[key] = request, age
        return True

    def start(self):
        if not self.tasks:
            self.tasks = [asyncio.create_task(self._worker(), name=f"review-worker-{i}")
                          for i in range(self.concurrency)]

    async def _worker(self):
        while not self.closed:
            key = next((key for key, (_, _, due) in self.deferred.items()
                        if due <= time.monotonic() and key not in self.active and key not in self.unapplied), None)
            retry = key is not None
            if key is None:
                key = next((key for key in self.pending if key not in self.active and key not in self.unapplied and key not in self.deferred), None)
            if key is None:
                await asyncio.sleep(0.01)
                continue
            if retry:
                request, age, _ = self.deferred.pop(key)
            else:
                request, age = self.pending.pop(key)
            self.active[key] = request
            result, error = None, None
            try:
                try:
                    result = await asyncio.wait_for(self.handler(request), self.timeout)
                except ReviewDeferred as exc:
                    self.deferred[key] = request, age, time.monotonic() + exc.retry_after
                    continue
                except Exception as exc:
                    self.failures += 1
                    error = exc
                try:
                    await self.completion(request, result, error)
                except Exception:
                    # Retain the audit result and pause this key. Other cameras
                    # may proceed; manual retry does not call the model again.
                    self.unapplied[key] = request, result, error
                    self.failures += 1
            finally:
                self.active.pop(key, None)

    async def retry_completion(self, camera_id: str, kind: str) -> bool:
        key = camera_id, kind
        item = self.unapplied.get(key)
        if item is None or key in self.active:
            return False
        self.active[key] = item[0]
        try:
            await self.completion(*item)
            self.unapplied.pop(key)
            return True
        finally:
            self.active.pop(key, None)

    def status(self):
        ages = [age for _, age in self.pending.values()]
        ages.extend(age for _, age, _ in self.deferred.values())
        return {"pending": len(set(self.pending) | set(self.deferred)), "deferred": len(self.deferred), "running": len(self.active),
                "oldest_age_seconds": time.monotonic() - min(ages) if ages else 0,
                "failures": self.failures,
                "unapplied_results": len(self.unapplied),
                "failed_workers": sum(t.done() and not t.cancelled() for t in self.tasks)}

    async def close(self):
        self.closed = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def atomic_evidence(root: Path, reference: str, jpeg: bytes) -> None:
    """Deterministic destination, same-directory atomic replace and fsync."""
    if Path(reference).name != reference or reference in {"", ".", ".."} or "\\" in reference:
        raise ValueError("Evidence reference must be a filename")
    root.mkdir(parents=True, exist_ok=True)
    destination = root / reference
    temporary = root / f".{reference}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(jpeg)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        if os.name != "nt":
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)
