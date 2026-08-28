from datetime import timedelta

import pytest

from backend import models
from backend.cleanup import CleanupService
from backend.config import Settings
from backend.database import Base, engine, session_scope, utc_now
from backend.repository import Repository


@pytest.fixture
def evidence_dir(tmp_path):
    directory = tmp_path / "evidence"
    directory.mkdir()
    return directory


def _add_camera(camera_id: str) -> None:
    with session_scope() as session:
        session.add(models.Camera(id=camera_id, name=camera_id, rtsp_url_encrypted="encrypted"))


def _add_alert(
    camera_id: str,
    mode: str,
    severity: str,
    created_at,
    evidence_path: str | None = None,
) -> int:
    with session_scope() as session:
        row = models.Alert(
            camera_id=camera_id,
            mode=mode,
            status="confirmed",
            severity=severity,
            created_at=created_at,
            evidence_path=evidence_path,
        )
        session.add(row)
        session.flush()
        session.refresh(row)
        return row.id


def _cleanup_camera(camera_id: str) -> None:
    with session_scope() as session:
        camera = session.get(models.Camera, camera_id)
        if camera:
            session.delete(camera)


def _run(evidence_dir, override_days=None, severity=None):
    settings = Settings(evidence_dir=evidence_dir)
    return CleanupService(settings, Repository()).run(override_days, severity)


def test_cleanup_removes_expired_alerts_and_evidence(evidence_dir):
    Base.metadata.create_all(engine)
    camera_id = "test-cleanup-expired"
    old_file = evidence_dir / "old.jpg"
    new_file = evidence_dir / "new.jpg"
    old_file.write_bytes(b"old")
    new_file.write_bytes(b"new")
    _add_camera(camera_id)
    old_alert = _add_alert(camera_id, "off_duty", "high", utc_now() - timedelta(days=40), "old.jpg")
    new_alert = _add_alert(camera_id, "off_duty", "high", utc_now(), "new.jpg")
    try:
        result = _run(evidence_dir)
        assert result["deleted"] == 1
        assert result["evidence_removed"] == 1
        assert result["days"] == 30
        with session_scope() as session:
            assert session.get(models.Alert, old_alert) is None
            assert session.get(models.Alert, new_alert) is not None
        assert not old_file.exists()
        assert new_file.exists()
    finally:
        _cleanup_camera(camera_id)


def test_cleanup_honors_override_days(evidence_dir):
    Base.metadata.create_all(engine)
    camera_id = "test-cleanup-override"
    file = evidence_dir / "mid.jpg"
    file.write_bytes(b"mid")
    _add_camera(camera_id)
    alert_id = _add_alert(camera_id, "black_screen", "normal", utc_now() - timedelta(days=20), "mid.jpg")
    try:
        result = _run(evidence_dir, override_days=10)
        assert result["deleted"] == 1
        assert not file.exists()
        _add_alert(camera_id, "black_screen", "normal", utc_now() - timedelta(days=20), "mid.jpg")
        result = _run(evidence_dir, override_days=60)
        assert result["deleted"] == 0
        with session_scope() as session:
            assert session.get(models.Alert, alert_id) is not None
    finally:
        _cleanup_camera(camera_id)


def test_cleanup_skips_out_of_bounds_evidence(evidence_dir, tmp_path):
    Base.metadata.create_all(engine)
    camera_id = "test-cleanup-traversal"
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"secret")
    _add_camera(camera_id)
    alert_id = _add_alert(camera_id, "intrusion", "critical", utc_now() - timedelta(days=40), "../outside.jpg")
    try:
        result = _run(evidence_dir)
        assert result["deleted"] == 1
        assert result["evidence_removed"] == 0
        assert outside.exists()
        with session_scope() as session:
            assert session.get(models.Alert, alert_id) is None
    finally:
        _cleanup_camera(camera_id)


def test_cleanup_filters_by_severity(evidence_dir):
    Base.metadata.create_all(engine)
    camera_id = "test-cleanup-severity"
    critical_file = evidence_dir / "critical.jpg"
    normal_file = evidence_dir / "normal.jpg"
    critical_file.write_bytes(b"critical")
    normal_file.write_bytes(b"normal")
    _add_camera(camera_id)
    critical_id = _add_alert(camera_id, "fire_smoke", "critical", utc_now() - timedelta(days=40), "critical.jpg")
    normal_id = _add_alert(camera_id, "off_duty", "normal", utc_now() - timedelta(days=40), "normal.jpg")
    try:
        result = _run(evidence_dir, severity="critical")
        assert result["deleted"] == 1
        assert result["evidence_removed"] == 1
        assert not critical_file.exists()
        assert normal_file.exists()
        with session_scope() as session:
            assert session.get(models.Alert, critical_id) is None
            assert session.get(models.Alert, normal_id) is not None
    finally:
        _cleanup_camera(camera_id)


def test_cleanup_tolerates_missing_evidence_file(evidence_dir):
    Base.metadata.create_all(engine)
    camera_id = "test-cleanup-missing"
    _add_camera(camera_id)
    alert_id = _add_alert(camera_id, "phone_use", "high", utc_now() - timedelta(days=40), "missing.jpg")
    try:
        result = _run(evidence_dir)
        assert result["deleted"] == 1
        assert result["evidence_removed"] == 0
        with session_scope() as session:
            assert session.get(models.Alert, alert_id) is None
    finally:
        _cleanup_camera(camera_id)


def test_retention_settings_get_or_create_defaults():
    Base.metadata.create_all(engine)
    repository = Repository()
    row = repository.get_retention_settings()
    assert row.alert_retention_days == 30
    assert row.auto_cleanup_enabled is True
    saved = repository.save_retention_settings({"alert_retention_days": 7, "auto_cleanup_enabled": False})
    assert saved.alert_retention_days == 7
    assert saved.auto_cleanup_enabled is False
    repository.save_retention_settings({"alert_retention_days": 30, "auto_cleanup_enabled": True})
