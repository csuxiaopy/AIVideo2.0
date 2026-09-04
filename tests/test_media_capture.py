from types import SimpleNamespace

import pytest

from backend.media_capture import LivePreviewStream, MediaGateway, PreviewLimitError
from backend.pipeline import staggered_capture_times
from backend.schemas import CameraCreate, CameraOptions
from backend.schemas import Detection


def test_frame_interval_defaults_and_preserves_legacy_detector_options():
    payload = CameraCreate(
        id="camera-1",
        name="Camera 1",
        rtsp_url="rtsp://example.test/stream",
        modes=["black_screen"],
        options=CameraOptions(yolo_fps=0.5, fire_smoke_fps=2),
    )
    assert payload.frame_interval_seconds == 1
    assert payload.options.yolo_fps == 0.5
    assert payload.options.fire_smoke_fps == 2


def test_96_cameras_are_evenly_staggered_over_60_seconds():
    cameras = [
        SimpleNamespace(id=f"camera-{index:03d}", enabled=True, frame_interval_seconds=60)
        for index in range(96)
    ]
    times = sorted(staggered_capture_times(cameras, 100.0).values())
    assert len(times) == 96
    assert times[0] == 100.0
    assert times[-1] < 160.0
    assert times[1] - times[0] == pytest.approx(0.625)


@pytest.mark.asyncio
async def test_periodic_capture_saves_snapshot_without_creating_preview(tmp_path, monkeypatch):
    updates = []
    gateway = MediaGateway(lambda *args: updates.append(args), tmp_path)
    await gateway.sync([("camera-1", "rtsp://example.test/stream", True)])

    async def fake_grab(_source: str) -> bytes:
        return b"\xff\xd8frame\xff\xd9"

    monkeypatch.setattr(gateway, "_grab_single_frame", fake_grab)
    packet = await gateway.capture("camera-1")
    assert (tmp_path / "camera-1.jpg").read_bytes() == packet.jpeg
    assert gateway.previews == {}
    assert updates[-1][1] is True


@pytest.mark.asyncio
async def test_preview_limit_and_last_session_release(tmp_path, monkeypatch):
    gateway = MediaGateway(lambda *_: None, tmp_path, max_live_previews=1)
    await gateway.sync(
        [
            ("camera-1", "rtsp://example.test/one", True),
            ("camera-2", "rtsp://example.test/two", True),
        ]
    )
    monkeypatch.setattr(LivePreviewStream, "start", lambda self: setattr(self, "running", True))

    async def fake_stop(self):
        self.running = False
        self.sessions.clear()
        self.frame = None

    monkeypatch.setattr(LivePreviewStream, "stop", fake_stop)
    session = await gateway.start_preview("camera-1")
    with pytest.raises(PreviewLimitError, match="达到上限"):
        await gateway.start_preview("camera-2")
    assert len(gateway.previews) == 1
    await gateway.stop_preview(str(session["session_id"]))
    assert gateway.previews == {}


def test_preview_session_expires_after_timeout():
    stream = LivePreviewStream("camera-1", "rtsp://example.test/one", 2, 60)
    session_id = stream.add_session()
    heartbeat_at = stream.sessions[session_id]
    assert stream.expire_sessions(heartbeat_at + 59) == []
    assert stream.expire_sessions(heartbeat_at + 61) == [session_id]


def test_periodic_capture_keeps_source_resolution(monkeypatch, tmp_path):
    captured_command = []

    async def fake_subprocess(*command, **_kwargs):
        captured_command.extend(command)

        class Process:
            returncode = 0

            async def communicate(self):
                return b"\xff\xd8full-resolution\xff\xd9", b""

        return Process()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_subprocess)
    gateway = MediaGateway(lambda *_: None, tmp_path)
    jpeg = __import__("asyncio").run(gateway._grab_single_frame("file:///camera.mp4"))
    assert jpeg.startswith(b"\xff\xd8")
    assert "-vf" not in captured_command


def test_phone_overlay_expires_after_three_seconds(tmp_path, monkeypatch):
    gateway = MediaGateway(lambda *_: None, tmp_path)
    gateway.set_object_detections(
        "camera-1",
        [Detection(class_id=73, class_name="cell phone", confidence=0.8, box=(0.1, 0.1, 0.3, 0.4))],
    )
    created_at = gateway.object_overlays["camera-1"][0]
    monkeypatch.setattr("backend.media_capture.time.monotonic", lambda: created_at + 3.01)

    image = __import__("numpy").full((120, 160, 3), 24, dtype=__import__("numpy").uint8)
    ok, encoded = __import__("cv2").imencode(".jpg", image)
    assert ok
    jpeg = encoded.tobytes()
    assert gateway._decorate("camera-1", jpeg) == jpeg
