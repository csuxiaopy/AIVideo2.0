import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from backend.alerts import AlertService
from backend.cleanup import CleanupService
from backend.config import Settings
from backend.pipeline import MonitoringRuntime
from backend.rules import RuleStateRegistry


def _camera(camera_id: str, directory_id: int | None, mode: str, cooldown: int = 300):
    return SimpleNamespace(
        id=camera_id,
        name=f"camera-{camera_id}",
        directory_id=directory_id,
        enabled=True,
        online=True,
        modes_json=f'["{mode}"]',
        options_json=f'{{"alert_cooldown_seconds":{cooldown}}}',
        schedule_json=(
            '{"timezone":"UTC","weekly":'
            '{"3":[{"start":"00:00","end":"23:59"}]},"holidays":[]}'
        ),
    )


def test_off_duty_waits_for_every_directory_member_and_persists_every_image():
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    cameras = [_camera("a", 7, "off_duty", 120), _camera("b", 7, "off_duty", 300)]
    calls = []

    class Repository:
        def list_cameras(self):
            return cameras

        def latest_directory_alert_time(self, *_args):
            return None

        def add_analysis(self, **values):
            return SimpleNamespace(id=9, zone_name=None, local_model=None, model_version=None, **values)

    class Alerts:
        async def create_group(self, *args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(id=1)

    runtime = object.__new__(MonitoringRuntime)
    runtime.repository = Repository()
    runtime.alerts = Alerts()
    runtime.rules = RuleStateRegistry()
    runtime.directory_locks = {}
    runtime.yolo = SimpleNamespace(model_name="yolo")
    first = runtime.rules.for_camera("a")
    first.absence_since = now - timedelta(minutes=20)
    first.absence_alerted = True
    first.record_off_duty_review(True, now, b"image-a", 0.94)

    analysis = SimpleNamespace(
        id=1, mode="off_duty", severity="normal", zone_name=None,
        local_model="yolo", model_version="yolo",
    )
    assert not asyncio.run(runtime._maybe_create_off_duty_alert(cameras[0], analysis, now))
    assert calls == []

    second = runtime.rules.for_camera("b")
    second.absence_since = now - timedelta(minutes=10)
    second.absence_alerted = True
    second.record_off_duty_review(True, now, b"image-b", 0.87)
    assert asyncio.run(runtime._maybe_create_off_duty_alert(cameras[1], analysis, now))

    args, kwargs = calls[0]
    assert [item["camera_id"] for item in args[2]] == ["a", "b"]
    assert [item["jpeg"] for item in args[2]] == [b"image-a", b"image-b"]
    assert args[4] == 0.87
    assert args[5] == 300
    assert kwargs["event_started_at"] == second.absence_since
    assert not first.absence_vlm_confirmed
    assert not second.absence_vlm_confirmed


def test_off_duty_directory_cooldown_keeps_fresh_confirmations_for_later():
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    camera = _camera("a", None, "off_duty", 600)

    class Repository:
        def list_cameras(self):
            return [camera]

        def latest_directory_alert_time(self, *_args):
            return datetime.now(timezone.utc) - timedelta(seconds=30)

        def add_analysis(self, **_values):
            raise AssertionError("cooldown check must happen before creating a synthetic analysis")

    runtime = object.__new__(MonitoringRuntime)
    runtime.repository = Repository()
    runtime.alerts = SimpleNamespace(create_group=None)
    runtime.rules = RuleStateRegistry()
    runtime.directory_locks = {}
    runtime.yolo = SimpleNamespace(model_name="yolo")
    state = runtime.rules.for_camera(camera.id)
    state.absence_since = now - timedelta(minutes=20)
    state.absence_alerted = True
    state.record_off_duty_review(True, now, b"new-image", 0.9)

    assert not asyncio.run(runtime._maybe_create_off_duty_alert(camera, None, now))
    assert state.absence_vlm_confirmed
    assert state.absence_evidence_jpeg == b"new-image"


def test_off_duty_offline_member_blocks_directory_alert():
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    cameras = [_camera("a", 3, "off_duty"), _camera("b", 3, "off_duty")]
    cameras[1].online = False
    calls = []

    class Repository:
        def list_cameras(self):
            return cameras

        def latest_directory_alert_time(self, *_args):
            return None

    runtime = object.__new__(MonitoringRuntime)
    runtime.repository = Repository()
    runtime.alerts = SimpleNamespace(create_group=lambda *_args, **_kwargs: calls.append(1))
    runtime.rules = RuleStateRegistry()
    runtime.directory_locks = {}
    for camera in cameras:
        state = runtime.rules.for_camera(camera.id)
        state.absence_since = now - timedelta(minutes=20)
        state.absence_alerted = True
        state.record_off_duty_review(True, now, camera.id.encode(), 0.9)

    assert not asyncio.run(runtime._maybe_create_off_duty_alert(cameras[0], None, now))
    assert calls == []


def test_phone_alert_uses_longest_directory_cooldown():
    cameras = [_camera("a", 2, "phone_use", 60), _camera("b", 2, "phone_use", 900)]
    received = []

    class Repository:
        def list_cameras(self):
            return cameras

    class Alerts:
        async def create(self, *_args, **kwargs):
            received.append(kwargs)

    runtime = object.__new__(MonitoringRuntime)
    runtime.repository = Repository()
    runtime.alerts = Alerts()
    runtime.directory_locks = {}
    analysis = SimpleNamespace()
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    asyncio.run(runtime._create_phone_alert(
        cameras[0], analysis, b"phone", "threshold", now - timedelta(minutes=10), now
    ))
    assert received[0]["directory_cooldown_seconds"] == 900


def test_alert_service_snapshots_directory_name_and_publishes_all_evidence(tmp_path):
    published = []

    class Repository:
        def get_camera_directory(self, directory_id):
            assert directory_id == 4
            return SimpleNamespace(name="滨湖厅")

        def latest_directory_alert_time(self, *_args):
            return None

        def add_alert(self, evidences, **values):
            return SimpleNamespace(
                id=8,
                evidences=[SimpleNamespace(**item) for item in evidences],
                **values,
            )

        def list_webhook_targets(self, enabled_only=False):
            return []

    class EventBus:
        async def publish(self, payload):
            published.append(payload)

    camera = _camera("a", 4, "off_duty")
    analysis = SimpleNamespace(
        id=3, mode="off_duty", severity="normal", zone_name=None,
        local_model="yolo", model_version="yolo",
    )
    service = AlertService(
        Settings(evidence_dir=tmp_path), Repository(), None, EventBus(), None
    )
    alert = asyncio.run(service.create_group(
        camera,
        analysis,
        [
            {"camera_id": "a", "camera_name": "东侧", "confidence": 0.95, "jpeg": b"a"},
            {"camera_id": "b", "camera_name": "西侧", "confidence": 0.91, "jpeg": b"b"},
        ],
        "目录内 2 个监控源均经大模型确认离岗",
        0.91,
        300,
    ))
    assert alert.alert_name == "滨湖厅营业厅视频"
    assert len(alert.evidences) == 2
    assert all((tmp_path / item.evidence_path).is_file() for item in alert.evidences)
    assert published[0]["camera_name"] == "滨湖厅营业厅视频"
    assert [item["camera_name"] for item in published[0]["evidences"]] == ["东侧", "西侧"]


def test_cleanup_removes_all_directory_evidence_files(tmp_path):
    for name in ("a.jpg", "b.jpg"):
        (tmp_path / name).write_bytes(name.encode())
    row = SimpleNamespace(
        evidence_path="a.jpg",
        evidences=[SimpleNamespace(evidence_path="a.jpg"), SimpleNamespace(evidence_path="b.jpg")],
    )

    class Repository:
        def get_retention_settings(self):
            return SimpleNamespace(
                alert_retention_days=30, log_retention_days=30, auto_cleanup_enabled=True
            )

        def list_alerts_before(self, *_args):
            return [row]

        def delete_alerts_before(self, *_args):
            return 1

    result = CleanupService(Settings(evidence_dir=tmp_path), Repository()).run()
    assert result["evidence_removed"] == 2
    assert not (tmp_path / "a.jpg").exists()
    assert not (tmp_path / "b.jpg").exists()
