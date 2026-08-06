from backend import models
from backend.database import Base, engine, session_scope
from backend.repository import Repository


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


def test_camera_delete_cascades_traffic_in_sqlite_dev_database():
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
