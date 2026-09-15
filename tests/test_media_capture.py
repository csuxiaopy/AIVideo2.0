from types import SimpleNamespace
from collections import defaultdict

import pytest

from backend.media_capture import (
    LivePreviewStream,
    MediaGateway,
    PersistentCaptureStream,
    PreviewLimitError,
)
from backend.pipeline import (
    FIRE_SMOKE_INTERVAL_SECONDS,
    OFF_DUTY_INTERVAL_SECONDS,
    PEOPLE_FLOW_INTERVAL_SECONDS,
    general_detection_interval,
    staggered_capture_times,
)
from backend.pipeline import MonitoringRuntime
from backend.schemas import CameraCreate, CameraOptions
from backend.schemas import Detection


def test_cuda_capture_samples_before_download():
    stream = PersistentCaptureStream("1", "rtsp://test/main", lambda *_: None,
                                     lambda *_: None, decode_device="1")
    command = stream.command()
    assert command[command.index("-hwaccel") + 1] == "cuda"
    assert command[command.index("-hwaccel_device") + 1] == "1"
    assert command.index("-hwaccel") < command.index("-i")
    assert command[command.index("-vf") + 1].startswith("fps=1.000,hwdownload,format=nv12,")
    assert stream.status()["decoder"] == "cuda"


def test_cpu_capture_keeps_software_filters():
    stream = PersistentCaptureStream("1", "rtsp://test/main", lambda *_: None, lambda *_: None)
    assert "-hwaccel" not in stream.command()
    assert "hwdownload" not in stream.command()[stream.command().index("-vf") + 1]


def test_gpu_allocation_is_bounded_balanced_and_stable(tmp_path):
    gateway = MediaGateway(lambda *_: None, tmp_path, capture_decode_devices="0,1",
                           capture_gpu_streams_per_device=2, capture_cpu_camera_ids="002")
    sources = {f"{i:03}": "rtsp://test/main" for i in range(7)}
    assigned = gateway.decode_assignments(sources)
    assert assigned["002"] is None
    assert list(assigned.values()).count("0") == 2
    assert list(assigned.values()).count("1") == 2
    assert list(assigned.values()).count(None) == 3
    assert assigned == gateway.decode_assignments(dict(reversed(list(sources.items()))))


def test_capture_gpu_disabled_by_default(tmp_path):
    gateway = MediaGateway(lambda *_: None, tmp_path)
    assert gateway.decode_assignments({"1": "rtsp://test/main"}) == {"1": None}


def test_fire_interval_is_per_camera(monkeypatch):
    runtime = MonitoringRuntime.__new__(MonitoringRuntime)
    runtime.next_fire_run = {"1": 105.0}
    monkeypatch.setattr("backend.pipeline.time.monotonic", lambda: 104.9)
    assert not runtime.fire_due("1")
    assert runtime.fire_due("2")
    monkeypatch.setattr("backend.pipeline.time.monotonic", lambda: 105.0)
    assert runtime.fire_due("1")


def test_fixed_mode_intervals_and_general_pipeline_cadence():
    camera = SimpleNamespace(frame_interval_seconds=60)
    assert PEOPLE_FLOW_INTERVAL_SECONDS == 1
    assert OFF_DUTY_INTERVAL_SECONDS == 10
    assert FIRE_SMOKE_INTERVAL_SECONDS == 30
    assert general_detection_interval(camera, {"people_flow"}) == 1
    assert general_detection_interval(camera, {"off_duty"}) == 10
    assert general_detection_interval(camera, {"people_flow", "off_duty"}) == 1
    assert general_detection_interval(camera, {"fire_smoke"}) is None
    assert general_detection_interval(camera, {"black_screen"}) == 60


@pytest.mark.asyncio
async def test_fire_only_camera_is_scheduled_without_general_task(monkeypatch):
    enqueued = []

    class Queue:
        def __init__(self, name):
            self.name = name

        async def enqueue(self, camera_id, priority):
            enqueued.append((self.name, camera_id, priority))
            return "task"

        async def depths(self):
            return {"critical": 0, "high": 0, "normal": 0, "low": 0}

    runtime = MonitoringRuntime.__new__(MonitoringRuntime)
    runtime.running = True
    runtime.repository = SimpleNamespace(list_cameras=lambda: [SimpleNamespace(
        id="fire-1", enabled=True, modes_json='["fire_smoke"]', frame_interval_seconds=1,
    )])
    frame = SimpleNamespace(sequence=1)
    runtime.media = SimpleNamespace(
        latest=lambda _camera_id: frame,
        capture_status=lambda: {},
    )
    runtime.queue = Queue("general")
    runtime.fire_queue = Queue("fire")
    runtime.next_run = {}
    runtime.next_fire_run = {"fire-1": 0.0}
    runtime.last_analyzed_sequence = defaultdict(int)
    runtime.last_fire_sequence = defaultdict(int)
    runtime.queued = set()
    runtime.fire_queued = set()
    runtime.scheduler_cursor = 0

    async def stop_after_iteration(_seconds):
        runtime.running = False

    monkeypatch.setattr("backend.pipeline.asyncio.sleep", stop_after_iteration)
    await runtime._scheduler()

    assert enqueued == [("fire", "fire-1", "critical")]


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

    await gateway._publish_frame("camera-1", b"\xff\xd8frame\xff\xd9")
    packet = gateway.latest("camera-1")
    assert packet is not None
    assert (tmp_path / "camera-1.jpg").read_bytes() == packet.jpeg
    assert gateway.previews == {}
    assert updates[-1][1] is True
    await gateway.close()


@pytest.mark.asyncio
async def test_latest_frame_slot_overwrites_old_frame(tmp_path):
    gateway = MediaGateway(lambda *_: None, tmp_path)
    await gateway._publish_frame("camera-1", b"\xff\xd8one\xff\xd9")
    await gateway._publish_frame("camera-1", b"\xff\xd8two\xff\xd9")
    assert gateway.latest("camera-1").jpeg == b"\xff\xd8two\xff\xd9"
    assert len(gateway.snapshots["camera-1"]) == 1
    assert gateway.overwritten_frames["camera-1"] == 1


@pytest.mark.asyncio
async def test_persistent_capture_parses_multiple_jpegs(monkeypatch):
    published = []

    class Reader:
        def __init__(self, chunks):
            self.chunks = list(chunks)
        async def read(self, _size=-1):
            return self.chunks.pop(0) if self.chunks else b""

    class Process:
        returncode = None
        stdout = Reader([b"noise\xff\xd8one\xff\xd9\xff\xd8tw", b"o\xff\xd9", b""])
        stderr = Reader([])
        def kill(self):
            self.returncode = -9
        async def wait(self):
            return self.returncode

    async def fake_subprocess(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_subprocess)
    stream = PersistentCaptureStream(
        "camera-1", "rtsp://example.test/sub", lambda _id, jpeg: published.append(jpeg),
        lambda *_: None,
    )
    stream.running = True
    with pytest.raises(RuntimeError, match="未返回画面"):
        await stream._read_process()
    assert published == [b"\xff\xd8one\xff\xd9", b"\xff\xd8two\xff\xd9"]


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
