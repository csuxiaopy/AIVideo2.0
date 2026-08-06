from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, desc, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from backend import models
from backend.database import session_scope, utc_now


def as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


class Repository:
    def list_cameras(self) -> list[models.Camera]:
        with session_scope() as session:
            return list(session.scalars(select(models.Camera).order_by(models.Camera.created_at)))

    def get_camera(self, camera_id: str) -> models.Camera | None:
        with session_scope() as session:
            return session.get(models.Camera, camera_id)

    def create_camera(self, camera: models.Camera) -> models.Camera:
        with session_scope() as session:
            session.add(camera)
        return camera

    def update_camera(self, camera_id: str, values: dict[str, Any]) -> models.Camera | None:
        with session_scope() as session:
            camera = session.get(models.Camera, camera_id)
            if not camera:
                return None
            for key, value in values.items():
                setattr(camera, key, value)
            camera.updated_at = utc_now()
        return camera

    def delete_camera(self, camera_id: str) -> bool:
        with session_scope() as session:
            camera = session.get(models.Camera, camera_id)
            if not camera:
                return False
            session.delete(camera)
        return True

    def set_camera_runtime(self, camera_id: str, online: bool, error: str | None = None) -> None:
        with session_scope() as session:
            camera = session.get(models.Camera, camera_id)
            if camera:
                camera.online = online
                camera.last_error = (error or "")[:1000] or None
                if online:
                    camera.last_seen_at = utc_now()

    def add_analysis(self, **values: Any) -> models.Analysis:
        row = models.Analysis(**values)
        with session_scope() as session:
            session.add(row)
            session.flush()
            session.refresh(row)
        return row

    def add_alert(self, **values: Any) -> models.Alert:
        row = models.Alert(**values)
        with session_scope() as session:
            session.add(row)
            session.flush()
            session.refresh(row)
        return row

    def update_alert_webhook(self, alert_id: int, status: str) -> None:
        with session_scope() as session:
            row = session.get(models.Alert, alert_id)
            if row:
                row.webhook_status = status

    def latest_alert_time(self, camera_id: str, mode: str) -> datetime | None:
        with session_scope() as session:
            return session.scalar(
                select(models.Alert.created_at)
                .where(models.Alert.camera_id == camera_id, models.Alert.mode == mode)
                .order_by(desc(models.Alert.created_at))
                .limit(1)
            )

    def list_alerts(
        self,
        limit: int = 100,
        camera_id: str | None = None,
        mode: str | None = None,
        severity: str | None = None,
    ):
        with session_scope() as session:
            priority = case(
                (models.Alert.severity == "critical", 0),
                (models.Alert.severity == "high", 1),
                (models.Alert.severity == "normal", 2),
                else_=3,
            )
            stmt = select(models.Alert).order_by(priority, desc(models.Alert.created_at)).limit(limit)
            if camera_id:
                stmt = stmt.where(models.Alert.camera_id == camera_id)
            if mode:
                stmt = stmt.where(models.Alert.mode == mode)
            if severity:
                stmt = stmt.where(models.Alert.severity == severity)
            return list(session.scalars(stmt))

    def list_analyses(self, limit: int = 100, camera_id: str | None = None):
        with session_scope() as session:
            stmt = select(models.Analysis).order_by(desc(models.Analysis.created_at)).limit(limit)
            if camera_id:
                stmt = stmt.where(models.Analysis.camera_id == camera_id)
            return list(session.scalars(stmt))

    def upsert_traffic(self, camera_id: str, current_count: int, entered: int, exited: int) -> None:
        now = utc_now().replace(second=0, microsecond=0)
        with session_scope() as session:
            row = session.scalar(
                select(models.TrafficAggregate).where(
                    models.TrafficAggregate.camera_id == camera_id,
                    models.TrafficAggregate.bucket_start == now,
                )
            )
            if not row:
                # SQLAlchemy column defaults are applied when INSERT is emitted,
                # so initialize counters before accumulating into a new bucket.
                row = models.TrafficAggregate(
                    camera_id=camera_id,
                    bucket_start=now,
                    current_count=0,
                    entered=0,
                    exited=0,
                )
                session.add(row)
            row.current_count = current_count
            row.entered = (row.entered or 0) + entered
            row.exited = (row.exited or 0) + exited

    def traffic(self, camera_id: str | None = None, limit: int = 1440):
        with session_scope() as session:
            stmt = select(models.TrafficAggregate).order_by(desc(models.TrafficAggregate.bucket_start)).limit(limit)
            if camera_id:
                stmt = stmt.where(models.TrafficAggregate.camera_id == camera_id)
            return list(session.scalars(stmt))

    def dashboard(self) -> dict[str, Any]:
        today = datetime.now(timezone.utc).date()
        with session_scope() as session:
            cameras = session.scalar(select(func.count()).select_from(models.Camera)) or 0
            online = session.scalar(select(func.count()).select_from(models.Camera).where(models.Camera.online)) or 0
            alerts = session.scalar(
                select(func.count()).select_from(models.Alert).where(func.date(models.Alert.created_at) == str(today))
            ) or 0
            failures = session.scalar(
                select(func.count()).select_from(models.Analysis).where(
                    models.Analysis.error.is_not(None), func.date(models.Analysis.created_at) == str(today)
                )
            ) or 0
            entered = session.scalar(
                select(func.coalesce(func.sum(models.TrafficAggregate.entered), 0)).where(
                    func.date(models.TrafficAggregate.bucket_start) == str(today)
                )
            ) or 0
            exited = session.scalar(
                select(func.coalesce(func.sum(models.TrafficAggregate.exited), 0)).where(
                    func.date(models.TrafficAggregate.bucket_start) == str(today)
                )
            ) or 0
            scene_rows = session.execute(
                select(models.Camera.scene_type, func.count()).group_by(models.Camera.scene_type)
            ).all()
            critical = session.scalar(
                select(func.count()).select_from(models.Alert).where(
                    models.Alert.severity == "critical", func.date(models.Alert.created_at) == str(today)
                )
            ) or 0
            intrusions = session.scalar(
                select(func.count()).select_from(models.Alert).where(
                    models.Alert.mode == "intrusion", func.date(models.Alert.created_at) == str(today)
                )
            ) or 0
            latest_traffic = list(
                session.scalars(select(models.TrafficAggregate).order_by(desc(models.TrafficAggregate.bucket_start)))
            )
            current_people = sum(
                row.current_count for row in {row.camera_id: row for row in reversed(latest_traffic)}.values()
            )
        return {
            "cameras": cameras,
            "online": online,
            "offline": max(0, cameras - online),
            "alerts_today": alerts,
            "failures_today": failures,
            "entered_today": int(entered),
            "exited_today": int(exited),
            "current_people": int(current_people),
            "critical_alerts_today": int(critical),
            "intrusions_today": int(intrusions),
            "scene_counts": {scene or "custom": int(count) for scene, count in scene_rows},
        }

    def get_model_settings(self) -> models.ModelSettings:
        with session_scope() as session:
            row = session.get(models.ModelSettings, 1)
            if not row:
                row = models.ModelSettings(id=1)
                session.add(row)
                session.flush()
                session.refresh(row)
            return row

    def save_model_settings(self, values: dict[str, Any]) -> models.ModelSettings:
        with session_scope() as session:
            row = session.get(models.ModelSettings, 1) or models.ModelSettings(id=1)
            session.add(row)
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = utc_now()
            session.flush()
            session.refresh(row)
            return row

    def get_webhook_settings(self) -> models.WebhookSettings:
        with session_scope() as session:
            row = session.get(models.WebhookSettings, 1)
            if not row:
                row = models.WebhookSettings(id=1)
                session.add(row)
                session.flush()
                session.refresh(row)
            return row

    def save_webhook_settings(self, values: dict[str, Any]) -> models.WebhookSettings:
        with session_scope() as session:
            row = session.get(models.WebhookSettings, 1) or models.WebhookSettings(id=1)
            session.add(row)
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = utc_now()
            session.flush()
            session.refresh(row)
            return row

    def get_detector_settings(self) -> models.DetectorSettings:
        with session_scope() as session:
            row = session.get(models.DetectorSettings, 1)
            if not row:
                row = models.DetectorSettings(id=1)
                session.add(row)
                session.flush()
                session.refresh(row)
            return row

    def save_detector_settings(self, values: dict[str, Any]) -> models.DetectorSettings:
        with session_scope() as session:
            row = session.get(models.DetectorSettings, 1) or models.DetectorSettings(id=1)
            session.add(row)
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = utc_now()
            session.flush()
            session.refresh(row)
            return row
