from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, delete, desc, func, select, update
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

    def create_cameras(self, cameras: list[models.Camera]) -> list[models.Camera]:
        """Insert a validated batch in one transaction."""
        with session_scope() as session:
            session.add_all(cameras)
            session.flush()
        return cameras

    def existing_camera_ids(self, camera_ids: list[str]) -> set[str]:
        if not camera_ids:
            return set()
        with session_scope() as session:
            return set(session.scalars(select(models.Camera.id).where(models.Camera.id.in_(camera_ids))))

    def rename_and_update_camera(
        self, camera_id: str, new_camera_id: str, values: dict[str, Any]
    ) -> models.Camera | None:
        """Rename the business key atomically; FK ON UPDATE CASCADE preserves history."""
        with session_scope() as session:
            if not session.get(models.Camera, camera_id):
                return None
            session.execute(
                update(models.Camera)
                .where(models.Camera.id == camera_id)
                .values(id=new_camera_id, updated_at=utc_now(), **values)
            )
        return self.get_camera(new_camera_id)

    def delete_camera(self, camera_id: str) -> bool:
        with session_scope() as session:
            camera = session.get(models.Camera, camera_id)
            if not camera:
                return False
            session.delete(camera)
        return True

    def set_camera_runtime(
        self,
        camera_id: str,
        online: bool,
        error: str | None = None,
        frame_at: datetime | None = None,
    ) -> None:
        with session_scope() as session:
            camera = session.get(models.Camera, camera_id)
            if camera:
                camera.online = online
                camera.last_error = (error or "")[:1000] or None
                if online:
                    camera.last_seen_at = utc_now()
                if frame_at is not None:
                    camera.last_frame_at = frame_at

    def set_last_analysis_at(self, camera_id: str, analyzed_at: datetime | None = None) -> None:
        with session_scope() as session:
            camera = session.get(models.Camera, camera_id)
            if camera:
                camera.last_analysis_at = analyzed_at or utc_now()

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

    def get_alert(self, alert_id: int) -> models.Alert | None:
        with session_scope() as session:
            return session.get(models.Alert, alert_id)

    def list_webhook_targets(self, enabled_only: bool = False) -> list[models.WebhookTarget]:
        with session_scope() as session:
            stmt = select(models.WebhookTarget).order_by(models.WebhookTarget.id)
            if enabled_only:
                stmt = stmt.where(models.WebhookTarget.enabled.is_(True))
            return list(session.scalars(stmt))

    def get_webhook_target(self, target_id: int) -> models.WebhookTarget | None:
        with session_scope() as session:
            return session.get(models.WebhookTarget, target_id)

    def create_webhook_target(self, values: dict[str, Any]) -> models.WebhookTarget:
        row = models.WebhookTarget(**values)
        with session_scope() as session:
            session.add(row)
            session.flush()
            session.refresh(row)
        return row

    def update_webhook_target(self, target_id: int, values: dict[str, Any]) -> models.WebhookTarget | None:
        with session_scope() as session:
            row = session.get(models.WebhookTarget, target_id)
            if not row:
                return None
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = utc_now()
            session.flush()
            session.refresh(row)
            return row

    def delete_webhook_target(self, target_id: int) -> bool:
        with session_scope() as session:
            row = session.get(models.WebhookTarget, target_id)
            if not row:
                return False
            session.delete(row)
            return True

    def upsert_webhook_delivery(
        self, alert_id: int, target: models.WebhookTarget, trigger: str, status: str = "pending", error: str | None = None
    ) -> models.WebhookDelivery:
        with session_scope() as session:
            row = session.scalar(select(models.WebhookDelivery).where(
                models.WebhookDelivery.alert_id == alert_id,
                models.WebhookDelivery.webhook_target_id == target.id,
            ))
            if not row:
                row = models.WebhookDelivery(alert_id=alert_id, webhook_target_id=target.id,
                    target_name=target.name, target_url=target.url, trigger=trigger, status=status)
                session.add(row)
            row.target_name = target.name
            row.target_url = target.url
            row.trigger = trigger
            row.status = status
            row.error = error
            row.updated_at = utc_now()
            session.flush()
            session.refresh(row)
            return row

    def update_webhook_delivery(self, delivery_id: int, status: str, error: str | None = None) -> None:
        with session_scope() as session:
            row = session.get(models.WebhookDelivery, delivery_id)
            if row:
                row.status = status
                row.error = error
                row.updated_at = utc_now()

    def webhook_deliveries(self, alert_id: int) -> list[models.WebhookDelivery]:
        with session_scope() as session:
            return list(session.scalars(select(models.WebhookDelivery).where(
                models.WebhookDelivery.alert_id == alert_id
            ).order_by(models.WebhookDelivery.id)))

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

    def traffic_summary(self, now: datetime | None = None) -> dict[str, Any]:
        """Build the business-day people-flow dashboard in Asia/Shanghai."""
        zone = ZoneInfo("Asia/Shanghai")
        current_time = (now or utc_now()).astimezone(zone)
        local_start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
        start = local_start.astimezone(timezone.utc)
        end = (local_start + timedelta(days=1)).astimezone(timezone.utc)

        with session_scope() as session:
            flow_cameras = [
                camera for camera in session.scalars(select(models.Camera).order_by(models.Camera.id))
                if "people_flow" in from_json(camera.modes_json, [])
            ]
            camera_ids = [camera.id for camera in flow_cameras]
            if not camera_ids:
                return {
                    "date": local_start.date().isoformat(), "timezone": str(zone),
                    "total_flow_today": 0, "current_people": 0, "entered_today": 0,
                    "exited_today": 0, "flow_camera_count": 0, "store_trend": [],
                    "cameras": [], "current_ranking": [], "flow_ranking": [],
                }

            today_rows = list(session.scalars(
                select(models.TrafficAggregate).where(
                    models.TrafficAggregate.camera_id.in_(camera_ids),
                    models.TrafficAggregate.bucket_start >= start,
                    models.TrafficAggregate.bucket_start < end,
                ).order_by(models.TrafficAggregate.bucket_start, models.TrafficAggregate.camera_id)
            ))
            ranked_prior = select(
                models.TrafficAggregate.id,
                func.row_number().over(
                    partition_by=models.TrafficAggregate.camera_id,
                    order_by=models.TrafficAggregate.bucket_start.desc(),
                ).label("row_number"),
            ).where(
                models.TrafficAggregate.camera_id.in_(camera_ids),
                models.TrafficAggregate.bucket_start < start,
            ).subquery()
            prior_rows = list(session.scalars(
                select(models.TrafficAggregate).join(
                    ranked_prior, models.TrafficAggregate.id == ranked_prior.c.id
                ).where(ranked_prior.c.row_number == 1)
            ))
            prior = {row.camera_id: row for row in prior_rows}

        latest: dict[str, models.TrafficAggregate] = {
            camera_id: row for camera_id, row in prior.items() if row is not None
        }
        entered = {camera_id: 0 for camera_id in camera_ids}
        exited = {camera_id: 0 for camera_id in camera_ids}
        for row in today_rows:
            latest[row.camera_id] = row
            entered[row.camera_id] += row.entered or 0
            exited[row.camera_id] += row.exited or 0

        # Replay today's camera updates and carry the last value of cameras that
        # did not report in the same minute, so the all-store line is not undercounted.
        state = {camera_id: 0 for camera_id in camera_ids}
        for camera_id, row in prior.items():
            state[camera_id] = row.current_count or 0
        trend: list[dict[str, Any]] = []
        grouped: dict[datetime, list[models.TrafficAggregate]] = {}
        for row in today_rows:
            grouped.setdefault(self._aware_utc(row.bucket_start), []).append(row)
        for bucket, bucket_rows in grouped.items():
            for row in bucket_rows:
                state[row.camera_id] = row.current_count or 0
            trend.append({"time": bucket, "current_people": int(sum(state.values()))})

        camera_items = []
        for camera in flow_cameras:
            last = latest.get(camera.id)
            camera_items.append({
                "camera_id": camera.id, "camera_name": camera.name, "online": camera.online,
                "current_count": int(last.current_count or 0) if last else 0,
                "entered_today": int(entered[camera.id]), "exited_today": int(exited[camera.id]),
                "last_stat_at": self._aware_utc(last.bucket_start) if last else None,
            })
        camera_items.sort(key=lambda item: (-item["entered_today"], item["camera_id"]))
        current_ranking = sorted(camera_items, key=lambda item: (-item["current_count"], item["camera_id"]))[:3]
        flow_ranking = camera_items[:3]
        entered_total = sum(entered.values())
        return {
            "date": local_start.date().isoformat(), "timezone": str(zone),
            "total_flow_today": int(entered_total),
            "current_people": int(sum(item["current_count"] for item in camera_items)),
            "entered_today": int(entered_total), "exited_today": int(sum(exited.values())),
            "flow_camera_count": len(flow_cameras), "store_trend": trend,
            "cameras": camera_items, "current_ranking": current_ranking, "flow_ranking": flow_ranking,
        }

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    def dashboard(self) -> dict[str, Any]:
        today = datetime.now(timezone.utc).date()
        today_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        tomorrow_start = today_start + timedelta(days=1)
        with session_scope() as session:
            cameras = session.scalar(select(func.count()).select_from(models.Camera)) or 0
            online = session.scalar(select(func.count()).select_from(models.Camera).where(models.Camera.online)) or 0
            alerts = session.scalar(
                select(func.count()).select_from(models.Alert).where(
                    models.Alert.created_at >= today_start, models.Alert.created_at < tomorrow_start
                )
            ) or 0
            failures = session.scalar(
                select(func.count()).select_from(models.Analysis).where(
                    models.Analysis.error.is_not(None),
                    models.Analysis.created_at >= today_start,
                    models.Analysis.created_at < tomorrow_start,
                )
            ) or 0
            entered = session.scalar(
                select(func.coalesce(func.sum(models.TrafficAggregate.entered), 0)).where(
                    models.TrafficAggregate.bucket_start >= today_start,
                    models.TrafficAggregate.bucket_start < tomorrow_start,
                )
            ) or 0
            exited = session.scalar(
                select(func.coalesce(func.sum(models.TrafficAggregate.exited), 0)).where(
                    models.TrafficAggregate.bucket_start >= today_start,
                    models.TrafficAggregate.bucket_start < tomorrow_start,
                )
            ) or 0
            scene_rows = session.execute(
                select(models.Camera.scene_type, func.count()).group_by(models.Camera.scene_type)
            ).all()
            critical = session.scalar(
                select(func.count()).select_from(models.Alert).where(
                    models.Alert.severity == "critical",
                    models.Alert.created_at >= today_start,
                    models.Alert.created_at < tomorrow_start,
                )
            ) or 0
            intrusions = session.scalar(
                select(func.count()).select_from(models.Alert).where(
                    models.Alert.mode == "intrusion",
                    models.Alert.created_at >= today_start,
                    models.Alert.created_at < tomorrow_start,
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

    def get_retention_settings(self) -> models.RetentionSettings:
        with session_scope() as session:
            row = session.get(models.RetentionSettings, 1)
            if not row:
                row = models.RetentionSettings(id=1)
                session.add(row)
                session.flush()
                session.refresh(row)
            return row

    def save_retention_settings(self, values: dict[str, Any]) -> models.RetentionSettings:
        with session_scope() as session:
            row = session.get(models.RetentionSettings, 1) or models.RetentionSettings(id=1)
            session.add(row)
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = utc_now()
            session.flush()
            session.refresh(row)
            return row

    def get_display_settings(self) -> models.DisplaySettings:
        with session_scope() as session:
            row = session.get(models.DisplaySettings, 1)
            if not row:
                row = models.DisplaySettings(id=1)
                session.add(row)
                session.flush()
                session.refresh(row)
            return row

    def save_display_settings(self, values: dict[str, Any]) -> models.DisplaySettings:
        with session_scope() as session:
            row = session.get(models.DisplaySettings, 1) or models.DisplaySettings(id=1)
            session.add(row)
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = utc_now()
            session.flush()
            session.refresh(row)
            return row

    def list_alerts_before(self, cutoff: datetime, severity: str | None = None) -> list[models.Alert]:
        with session_scope() as session:
            stmt = select(models.Alert).where(models.Alert.created_at < cutoff)
            if severity:
                stmt = stmt.where(models.Alert.severity == severity)
            return list(session.scalars(stmt))

    def delete_alerts_before(self, cutoff: datetime, severity: str | None = None) -> int:
        with session_scope() as session:
            stmt = delete(models.Alert).where(models.Alert.created_at < cutoff)
            if severity:
                stmt = stmt.where(models.Alert.severity == severity)
            result = session.execute(stmt)
            return result.rowcount or 0
