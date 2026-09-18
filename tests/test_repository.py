from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

import backend.repository as repository_module
from backend import models
from backend.database import Base, engine, session_scope
from backend.repository import Repository


def test_analysis_logs_filter_by_time_range_and_camera_name_snapshot(monkeypatch):
    test_engine = create_engine("sqlite://")
    Base.metadata.create_all(test_engine)
    session_factory = sessionmaker(bind=test_engine, expire_on_commit=False)

    @contextmanager
    def local_session_scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(repository_module, "session_scope", local_session_scope)
    repository = Repository()
    start = datetime(2026, 9, 17, 23, 30, tzinfo=timezone.utc)
    end = datetime(2026, 9, 18, 1, 0, tzinfo=timezone.utc)
    rows = [
        repository.add_analysis(
            camera_id=None, camera_name="Lobby East", mode="intrusion",
            status="confirmed", created_at=start,
        ),
        repository.add_analysis(
            camera_id=None, camera_name="lobby west", mode="intrusion",
            status="confirmed", created_at=end - timedelta(minutes=1),
        ),
        repository.add_analysis(
            camera_id=None, camera_name="Lobby East", mode="intrusion",
            status="confirmed", created_at=end,
        ),
        repository.add_analysis(
            camera_id=None, camera_name="Warehouse", mode="intrusion",
            status="confirmed", created_at=start + timedelta(minutes=1),
        ),
    ]
    try:
        matches, total = repository.list_analysis_logs(
            page=1, page_size=50, start=start, end=end, camera_name="LOBBY"
        )
        assert total == 2
        assert {row.id for row in matches} == {rows[0].id, rows[1].id}
    finally:
        with local_session_scope() as session:
            session.execute(delete(models.Analysis).where(
                models.Analysis.id.in_([row.id for row in rows])
            ))


def test_first_traffic_bucket_initializes_counters():
    Base.metadata.create_all(engine)
    camera_id = "test-traffic-initialization"

    with session_scope() as session:
        existing = session.get(models.Camera, camera_id)
        if existing:
            session.delete(existing)
        session.add(
            models.Camera(
                id=camera_id,
                name="traffic test",
                rtsp_url_encrypted="encrypted",
            )
        )

    repository = Repository()
    repository.upsert_traffic(camera_id, current_count=2, entered=1, exited=0)
    rows = repository.traffic(camera_id=camera_id, limit=1)

    assert rows[0].current_count == 2
    assert rows[0].entered == 1
    assert rows[0].exited == 0

    with session_scope() as session:
        camera = session.get(models.Camera, camera_id)
        if camera:
            session.delete(camera)


def test_camera_delete_cascades_traffic_in_postgresql_dev_database():
    Base.metadata.create_all(engine)
    camera_id = "test-traffic-cascade"
    with session_scope() as session:
        session.add(
            models.Camera(
                id=camera_id,
                name="cascade test",
                rtsp_url_encrypted="encrypted",
            )
        )

    repository = Repository()
    repository.upsert_traffic(camera_id, current_count=1, entered=1, exited=0)
    assert repository.delete_camera(camera_id)
    assert repository.traffic(camera_id=camera_id, limit=10) == []


def test_camera_batch_delete_is_atomic_and_reports_existing_ids():
    Base.metadata.create_all(engine)
    camera_ids = ["test-batch-delete-a", "test-batch-delete-b"]
    with session_scope() as session:
        session.add_all([
            models.Camera(id=camera_id, name=camera_id, rtsp_url_encrypted="encrypted")
            for camera_id in camera_ids
        ])
    repository = Repository()
    deleted = repository.delete_cameras([*camera_ids, "test-batch-delete-missing"])
    assert set(deleted) == set(camera_ids)
    assert all(repository.get_camera(camera_id) is None for camera_id in camera_ids)


def test_update_cameras_schedule_only_updates_requested_cameras():
    Base.metadata.create_all(engine)
    repository = Repository()
    camera_ids = ["test-schedule-target", "test-schedule-untouched"]
    with session_scope() as session:
        for camera_id in camera_ids:
            old = session.get(models.Camera, camera_id)
            if old:
                session.delete(old)
        session.add_all([
            models.Camera(id=camera_id, name=camera_id, rtsp_url_encrypted="encrypted")
            for camera_id in camera_ids
        ])
    try:
        schedule_json = '{"timezone":"Asia/Shanghai","weekly":{"0":[]},"holidays":[]}'
        assert repository.update_cameras_schedule([camera_ids[0]], schedule_json) == 1
        assert repository.get_camera(camera_ids[0]).schedule_json == schedule_json
        assert repository.get_camera(camera_ids[1]).schedule_json == "{}"
        assert repository.update_cameras_schedule([], schedule_json) == 0
    finally:
        for camera_id in camera_ids:
            repository.delete_camera(camera_id)


def test_camera_directory_counts_moves_and_unassigns_on_delete():
    Base.metadata.create_all(engine)
    repository = Repository()
    camera_id = "test-camera-directory"
    with session_scope() as session:
        old = session.get(models.Camera, camera_id)
        if old:
            session.delete(old)
    directory = repository.create_camera_directory(f"test-directory-{uuid4().hex}")
    try:
        with session_scope() as session:
            session.add(models.Camera(id=camera_id, name="directory camera", rtsp_url_encrypted="encrypted"))
        assert repository.move_cameras([camera_id], directory.id) == 1
        item = next(row for row in repository.list_camera_directories() if row["id"] == directory.id)
        assert item["camera_count"] == 1
        assert repository.delete_camera_directory(directory.id)
        assert repository.get_camera(camera_id).directory_id is None
    finally:
        repository.delete_camera(camera_id)


def test_alert_date_filter_uses_shanghai_day_boundaries():
    Base.metadata.create_all(engine)
    repository = Repository()
    camera_id = "test-alert-date-filter"
    with session_scope() as session:
        old = session.get(models.Camera, camera_id)
        if old:
            session.delete(old)
        session.add(models.Camera(id=camera_id, name="date camera", rtsp_url_encrypted="encrypted"))
    try:
        first = repository.add_alert(
            camera_id=camera_id, mode="intrusion", severity="high",
            created_at=datetime(2026, 9, 9, 15, 59, tzinfo=timezone.utc),
        )
        second = repository.add_alert(
            camera_id=camera_id, mode="intrusion", severity="high",
            created_at=datetime(2026, 9, 9, 16, 0, tzinfo=timezone.utc),
        )
        assert [row.id for row in repository.list_alerts(None, camera_id=camera_id, alert_date=date(2026, 9, 9))] == [first.id]
        assert [row.id for row in repository.list_alerts(None, camera_id=camera_id, alert_date=date(2026, 9, 10))] == [second.id]
    finally:
        repository.delete_camera(camera_id)


def test_alert_pagination_is_filtered_and_stable():
    Base.metadata.create_all(engine)
    repository = Repository()
    camera_id = f"test-alert-pagination-{uuid4().hex}"
    with session_scope() as session:
        session.add(models.Camera(id=camera_id, name="page camera", rtsp_url_encrypted="encrypted"))
    try:
        with session_scope() as session:
            created = [models.Alert(
                camera_id=camera_id,
                mode="off_duty" if index < 201 else "intrusion",
                severity="normal",
                created_at=datetime(2026, 9, 12, 4, 0, tzinfo=timezone.utc),
            ) for index in range(205)]
            session.add_all(created)
            session.flush()
            expected_ids = sorted((row.id for row in created[:201]), reverse=True)
        first, total = repository.list_alerts_page(1, 100, camera_id=camera_id, mode="off_duty")
        second, _ = repository.list_alerts_page(2, 100, camera_id=camera_id, mode="off_duty")
        third, _ = repository.list_alerts_page(3, 100, camera_id=camera_id, mode="off_duty")

        assert total == 201
        assert [len(first), len(second), len(third)] == [100, 100, 1]
        ids = [row.id for row in first + second + third]
        assert ids == expected_ids
        assert len(ids) == len(set(ids))
    finally:
        with session_scope() as session:
            session.execute(delete(models.Alert).where(models.Alert.camera_id == camera_id))
            camera = session.get(models.Camera, camera_id)
            if camera:
                session.delete(camera)
