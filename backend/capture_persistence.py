"""Latest-only capture I/O, owned by the event loop and isolated from inference."""
from __future__ import annotations

import asyncio
from collections import OrderedDict

from backend.background import IOPool, LatestWriter, atomic_evidence
from backend.database import utc_now


class CapturePersistence:
    def __init__(self, io: IOPool, repository, snapshot_path, max_cameras=4096):
        self.io, self.repository, self.snapshot_path = io, repository, snapshot_path
        self.allowed: set[str] = set()
        self.latest: dict[str, dict] = {}
        self.snapshots: OrderedDict[str, tuple[int, bytes]] = OrderedDict()
        self.writer = LatestWriter(io, repository.set_camera_runtime_batch, max_cameras)
        self.max_cameras = max_cameras
        self.sequence = 0
        self.snapshot_failures = 0
        self.snapshot_errors: dict[str, str] = {}
        self.snapshot_task = None

    def register(self, camera_ids):
        wanted = set(camera_ids)
        if len(wanted) > self.max_cameras:
            raise ValueError("Capture persistence camera capacity exceeded")
        self.allowed = wanted
        for mapping in (self.latest, self.snapshots, self.writer.pending, self.snapshot_errors):
            for key in list(mapping):
                if key not in wanted:
                    mapping.pop(key, None)

    def status_update(self, camera_id, online, error=None, frame_at=None):
        if camera_id not in self.allowed:
            return
        values = self.latest.setdefault(camera_id, {})
        values.update(online=online, last_error=(error or "")[:1000] or None)
        if online:
            values["last_seen_at"] = utc_now().isoformat()
        if frame_at is not None:
            values["last_frame_at"] = frame_at.isoformat()
        if not self.writer.submit(camera_id, values):
            raise RuntimeError("Registered camera exceeded bounded status capacity")

    def analysis_update(self, camera_id, analyzed_at=None):
        if camera_id not in self.allowed:
            return
        values = self.latest.setdefault(camera_id, {})
        values["last_analysis_at"] = (analyzed_at or utc_now()).isoformat()
        if not self.writer.submit(camera_id, values):
            raise RuntimeError("Registered camera exceeded bounded status capacity")

    def publish(self, camera_id, packet):
        if camera_id not in self.allowed:
            return
        self.sequence += 1
        self.snapshots[camera_id] = self.sequence, packet.jpeg
        self.status_update(camera_id, True, None, packet.captured_at)

    async def flush_snapshot(self):
        if not self.snapshots:
            return False
        camera_id, (version, jpeg) = next(iter(self.snapshots.items()))
        try:
            path = self.snapshot_path(camera_id)
            await self.io.run(atomic_evidence, path.parent, path.name, jpeg)
        except Exception as exc:
            self.snapshot_failures += 1
            if camera_id in self.allowed:
                self.snapshot_errors[camera_id] = type(exc).__name__
            if camera_id in self.snapshots:
                self.snapshots.move_to_end(camera_id)
            return False
        current = self.snapshots.get(camera_id)
        if current is not None and current[0] == version:
            self.snapshots.pop(camera_id)
        elif current is not None:
            self.snapshots.move_to_end(camera_id)
        self.snapshot_errors.pop(camera_id, None)
        return True

    def start(self):
        self.writer.start()
        if not self.snapshot_task:
            self.snapshot_task = asyncio.create_task(self._snapshots(), name="latest-snapshot-writer")

    async def _snapshots(self):
        while True:
            if not await self.flush_snapshot():
                await asyncio.sleep(1 if self.snapshot_errors else 0.05)

    def status(self):
        return {"status_pending": len(self.writer.pending), "snapshot_pending": len(self.snapshots),
                "status_failures": self.writer.failures, "snapshot_failures": self.snapshot_failures,
                "status_error": self.writer.error, "snapshot_errors": dict(self.snapshot_errors),
                "degraded": bool(self.writer.error or self.snapshot_errors)}

    async def close(self):
        await self.writer.close()
        if self.snapshot_task:
            self.snapshot_task.cancel()
            await asyncio.gather(self.snapshot_task, return_exceptions=True)
