import asyncio
import threading
from types import SimpleNamespace

import pytest

from backend.background import IOPool
from backend.capture_persistence import CapturePersistence
from backend.database import utc_now
from backend.media_capture import MediaGateway
from backend.pipeline import MonitoringRuntime


@pytest.mark.asyncio
async def test_capture_publish_does_not_wait_for_database_or_snapshot(tmp_path):
    io = IOPool(1)
    entered, release = threading.Event(), threading.Event()
    batches = []

    def slow_write(batch):
        entered.set()
        assert release.wait(5)
        batches.append(batch)

    gateway = MediaGateway(lambda *_: pytest.fail("legacy callback invoked"), tmp_path)
    persistence = CapturePersistence(io, SimpleNamespace(set_camera_runtime_batch=slow_write), gateway._snapshot_path)
    persistence.register(["a"])
    gateway.frame_persistence = persistence.publish
    await gateway._publish_frame("a", b"first")
    flushing = asyncio.create_task(persistence.writer.flush())
    while not entered.is_set():
        await asyncio.sleep(0.001)
    for n in range(100):
        await asyncio.wait_for(gateway._publish_frame("a", str(n).encode()), 0.1)
    assert gateway.latest("a").jpeg == b"99"
    assert len(persistence.snapshots) == 1
    assert len(persistence.writer.pending) == 1
    assert not (tmp_path / "a.jpg").exists()
    release.set()
    await flushing
    await persistence.flush_snapshot()
    assert (tmp_path / "a.jpg").read_bytes() == b"99"
    await persistence.writer.flush()
    assert batches[-1][0][1]["last_frame_at"] == gateway.latest("a").captured_at.isoformat()
    await io.close()


@pytest.mark.asyncio
async def test_status_fields_merge_and_offline_wins(tmp_path):
    saved = []
    io = IOPool(1)
    persistence = CapturePersistence(io, SimpleNamespace(set_camera_runtime_batch=saved.extend), lambda key: tmp_path / f"{key}.jpg")
    persistence.register(["a"])
    now = utc_now()
    persistence.status_update("a", True, frame_at=now)
    persistence.analysis_update("a", now)
    persistence.status_update("a", False, "disconnected")
    await persistence.writer.flush()
    assert saved[0][1] == {"online": False, "last_error": "disconnected",
                           "last_seen_at": saved[0][1]["last_seen_at"],
                           "last_frame_at": now.isoformat(), "last_analysis_at": now.isoformat()}
    persistence.register([])
    assert not persistence.latest
    await io.close()


def test_async_capture_never_loads_old_disk_snapshot(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"old")
    gateway = MediaGateway(lambda *_: None, tmp_path)
    gateway.frame_persistence = lambda *_: None
    assert gateway.latest("a") is None


def test_person_frequency_is_independent_of_analysis_status(monkeypatch):
    runtime = MonitoringRuntime.__new__(MonitoringRuntime)
    stamp = [100.0]
    monkeypatch.setattr("backend.pipeline.time.monotonic", lambda: stamp[0])
    runtime._record_person_detection("a")
    stamp[0] = 102.0
    assert runtime.person_detection_status()["a"]["frequency_hz"] == 0.5
    stamp[0] = 161.0
    assert runtime.person_detection_status()["a"]["frequency_hz"] == 0.0
