from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
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
    def create_captcha(self, row: models.CaptchaChallenge) -> models.CaptchaChallenge:
        now = utc_now()
        with session_scope() as session:
            session.execute(delete(models.CaptchaChallenge).where(
                (models.CaptchaChallenge.expires_at <= now) | models.CaptchaChallenge.used.is_(True)
            ))
            session.add(row)
        return row

    def consume_captcha(self, challenge_id: str, answer_hash: str) -> bool:
        """Atomically consume a matching, unexpired challenge."""
        with session_scope() as session:
            result = session.execute(
                update(models.CaptchaChallenge)
                .where(
                    models.CaptchaChallenge.id == challenge_id,
                    models.CaptchaChallenge.answer_hash == answer_hash,
                    models.CaptchaChallenge.used.is_(False),
                    models.CaptchaChallenge.expires_at > utc_now(),
                )
                .values(used=True)
            )
            return result.rowcount == 1

    def invalidate_captcha(self, challenge_id: str) -> None:
        with session_scope() as session:
            session.execute(update(models.CaptchaChallenge).where(
                models.CaptchaChallenge.id == challenge_id
            ).values(used=True))

    def count_users(self) -> int:
        with session_scope() as session:
            return session.scalar(select(func.count()).select_from(models.User)) or 0

    def list_users(self) -> list[models.User]:
        with session_scope() as session:
            return list(session.scalars(select(models.User).order_by(models.User.created_at)))

    def get_user(self, user_id: int) -> models.User | None:
        with session_scope() as session:
            return session.get(models.User, user_id)

    def get_user_by_username(self, username: str) -> models.User | None:
        with session_scope() as session:
            return session.scalar(select(models.User).where(func.lower(models.User.username) == username.lower()))

    def create_user(self, user: models.User) -> models.User:
        with session_scope() as session:
            session.add(user)
            session.flush()
            session.refresh(user)
        return user

    def update_user(self, user_id: int, values: dict[str, Any]) -> models.User | None:
        with session_scope() as session:
            row = session.get(models.User, user_id)
            if not row:
                return None
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = utc_now()
            session.flush()
            session.refresh(row)
        return row

    def delete_user(self, user_id: int) -> bool:
        with session_scope() as session:
            row = session.get(models.User, user_id)
            if not row:
                return False
            session.delete(row)
        return True

    def create_user_session(self, row: models.UserSession) -> models.UserSession:
        with session_scope() as session:
            session.add(row)
            session.flush()
            session.refresh(row)
        return row

    def get_user_session(self, digest: str) -> tuple[models.UserSession, models.User] | None:
        with session_scope() as session:
            stmt = (select(models.UserSession, models.User)
                    .join(models.User, models.User.id == models.UserSession.user_id)
                    .where(models.UserSession.token_hash == digest))
            return session.execute(stmt).first()

    def touch_user_session(self, session_id: int, last_seen_at: datetime, expires_at: datetime) -> None:
        with session_scope() as session:
            session.execute(update(models.UserSession).where(models.UserSession.id == session_id)
                            .values(last_seen_at=last_seen_at, expires_at=expires_at))

    def delete_user_session(self, digest: str) -> None:
        with session_scope() as session:
            session.execute(delete(models.UserSession).where(models.UserSession.token_hash == digest))

    def delete_user_sessions(self, user_id: int) -> None:
        with session_scope() as session:
            session.execute(delete(models.UserSession).where(models.UserSession.user_id == user_id))

    def list_cameras(self) -> list[models.Camera]:
        with session_scope() as session:
            return list(session.scalars(select(models.Camera).order_by(models.Camera.created_at)))

    def list_camera_directories(self) -> list[dict[str, Any]]:
        with session_scope() as session:
            rows = session.execute(
                select(models.CameraDirectory, func.count(models.Camera.id))
                .outerjoin(models.Camera, models.Camera.directory_id == models.CameraDirectory.id)
                .group_by(models.CameraDirectory.id).order_by(models.CameraDirectory.name)
            ).all()
            return [{"id": row.id, "name": row.name, "camera_count": int(count),
                     "created_at": row.created_at, "updated_at": row.updated_at}
                    for row, count in rows]

    def get_camera_directory(self, directory_id: int) -> models.CameraDirectory | None:
        with session_scope() as session:
            return session.get(models.CameraDirectory, directory_id)

    def create_camera_directory(self, name: str) -> models.CameraDirectory:
        row = models.CameraDirectory(name=name)
        with session_scope() as session:
            session.add(row); session.flush(); session.refresh(row)
        return row

    def update_camera_directory(self, directory_id: int, name: str) -> models.CameraDirectory | None:
        with session_scope() as session:
            row = session.get(models.CameraDirectory, directory_id)
            if not row:
                return None
            row.name = name; row.updated_at = utc_now(); session.flush(); session.refresh(row)
        return row

    def delete_camera_directory(self, directory_id: int) -> bool:
        with session_scope() as session:
            row = session.get(models.CameraDirectory, directory_id)
            if not row:
                return False
            session.execute(update(models.Camera).where(models.Camera.directory_id == directory_id).values(directory_id=None))
            session.delete(row)
        return True

    def move_cameras(self, camera_ids: list[str], directory_id: int | None) -> int:
        with session_scope() as session:
            result = session.execute(update(models.Camera).where(models.Camera.id.in_(camera_ids)).values(
                directory_id=directory_id, updated_at=utc_now()))
            return result.rowcount

    def update_cameras_schedule(self, camera_ids: list[str], schedule_json: str) -> int:
        """Update multiple camera schedules atomically."""
        if not camera_ids:
            return 0
        with session_scope() as session:
            result = session.execute(
                update(models.Camera)
                .where(models.Camera.id.in_(camera_ids))
                .values(schedule_json=schedule_json, updated_at=utc_now())
            )
            return result.rowcount

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

    def delete_cameras(self, camera_ids: list[str]) -> list[str]:
        """Delete existing cameras atomically and return the IDs actually removed."""
        if not camera_ids:
            return []
        with session_scope() as session:
            existing = list(session.scalars(select(models.Camera.id).where(models.Camera.id.in_(camera_ids))))
            if existing:
                session.execute(delete(models.Camera).where(models.Camera.id.in_(existing)))
        return existing

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
        if "camera_name" not in values:
            camera = self.get_camera(values.get("camera_id", ""))
            values["camera_name"] = camera.name if camera else values.get("camera_id", "")
        row = models.Analysis(**values)
        with session_scope() as session:
            session.add(row)
            session.flush()
            session.refresh(row)
        return row

    def add_alert(self, evidences: list[dict[str, Any]] | None = None, **values: Any) -> models.Alert:
        row = models.Alert(**values)
        for evidence in evidences or []:
            row.evidences.append(models.AlertEvidence(**evidence))
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

    def list_alerts_by_ids(self, alert_ids: list[int]) -> list[models.Alert]:
        if not alert_ids:
            return []
        with session_scope() as session:
            return list(session.scalars(select(models.Alert).where(models.Alert.id.in_(alert_ids))))

    def delete_alerts_by_ids(self, alert_ids: list[int]) -> list[int]:
        """Delete existing alerts and their delivery rows atomically."""
        if not alert_ids:
            return []
        with session_scope() as session:
            existing = list(session.scalars(select(models.Alert.id).where(models.Alert.id.in_(alert_ids))))
            if existing:
                session.execute(delete(models.WebhookDelivery).where(models.WebhookDelivery.alert_id.in_(existing)))
                session.execute(delete(models.AlertEvidence).where(models.AlertEvidence.alert_id.in_(existing)))
                session.execute(delete(models.Alert).where(models.Alert.id.in_(existing)))
            existing_set = set(existing)
            return [alert_id for alert_id in alert_ids if alert_id in existing_set]

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

    def add_audit_log(self, **values: Any) -> models.AuditLog:
        row = models.AuditLog(**values)
        with session_scope() as session:
            session.add(row)
            session.flush()
            session.refresh(row)
        return row

    def add_model_call_log(self, **values: Any) -> models.ModelCallLog:
        row = models.ModelCallLog(**values)
        with session_scope() as session:
            session.add(row)
            session.flush()
            session.refresh(row)
        return row

    def get_log(self, category: str, row_id: int):
        model = {"audit": models.AuditLog, "analyses": models.Analysis,
                 "model-calls": models.ModelCallLog}.get(category)
        if model is None:
            return None
        with session_scope() as session:
            return session.get(model, row_id)

    def list_audit_logs(
        self, *, page: int, page_size: int, start: datetime | None = None,
        end: datetime | None = None, username: str | None = None,
        action: str | None = None, outcome: str | None = None,
    ) -> tuple[list[models.AuditLog], int]:
        filters = []
        if start:
            filters.append(models.AuditLog.created_at >= start)
        if end:
            filters.append(models.AuditLog.created_at < end)
        if username:
            filters.append(models.AuditLog.actor_username.ilike(f"%{username}%"))
        if action:
            filters.append(models.AuditLog.action.ilike(f"%{action}%"))
        if outcome:
            filters.append(models.AuditLog.outcome == outcome)
        with session_scope() as session:
            total = session.scalar(select(func.count()).select_from(models.AuditLog).where(*filters)) or 0
            stmt = (select(models.AuditLog).where(*filters).order_by(desc(models.AuditLog.created_at))
                    .offset((page - 1) * page_size).limit(page_size))
            return list(session.scalars(stmt)), total

    def list_analysis_logs(
        self, *, page: int, page_size: int, start: datetime | None = None,
        end: datetime | None = None, camera_id: str | None = None,
        mode: str | None = None, status: str | None = None,
    ) -> tuple[list[models.Analysis], int]:
        filters = []
        if start:
            filters.append(models.Analysis.created_at >= start)
        if end:
            filters.append(models.Analysis.created_at < end)
        if camera_id:
            filters.append(models.Analysis.camera_id == camera_id)
        if mode:
            filters.append(models.Analysis.mode == mode)
        if status:
            filters.append(models.Analysis.status == status)
        with session_scope() as session:
            total = session.scalar(select(func.count()).select_from(models.Analysis).where(*filters)) or 0
            stmt = (select(models.Analysis).where(*filters).order_by(desc(models.Analysis.created_at))
                    .offset((page - 1) * page_size).limit(page_size))
            return list(session.scalars(stmt)), total

    def list_model_call_logs(
        self, *, page: int, page_size: int, start: datetime | None = None,
        end: datetime | None = None, camera_id: str | None = None,
        model: str | None = None, stage: str | None = None,
        outcome: str | None = None,
    ) -> tuple[list[models.ModelCallLog], int]:
        filters = []
        if start:
            filters.append(models.ModelCallLog.created_at >= start)
        if end:
            filters.append(models.ModelCallLog.created_at < end)
        if camera_id:
            filters.append(models.ModelCallLog.camera_id == camera_id)
        if model:
            filters.append(models.ModelCallLog.model.ilike(f"%{model}%"))
        if stage:
            filters.append(models.ModelCallLog.stage == stage)
        if outcome:
            filters.append(models.ModelCallLog.outcome == outcome)
        with session_scope() as session:
            total = session.scalar(select(func.count()).select_from(models.ModelCallLog).where(*filters)) or 0
            stmt = (select(models.ModelCallLog).where(*filters)
                    .order_by(desc(models.ModelCallLog.created_at))
                    .offset((page - 1) * page_size).limit(page_size))
            return list(session.scalars(stmt)), total

    def delete_logs_before(self, cutoff: datetime) -> dict[str, int]:
        with session_scope() as session:
            counts = {}
            for key, model in (("audit", models.AuditLog), ("analyses", models.Analysis),
                               ("model_calls", models.ModelCallLog)):
                result = session.execute(delete(model).where(model.created_at < cutoff))
                counts[key] = result.rowcount or 0
            return counts

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

    def latest_directory_alert_time(self, directory_id: int | None, mode: str) -> datetime | None:
        with session_scope() as session:
            directory_filter = (
                models.Alert.directory_id.is_(None)
                if directory_id is None else models.Alert.directory_id == directory_id
            )
            return session.scalar(
                select(models.Alert.created_at)
                .where(directory_filter, models.Alert.mode == mode)
                .order_by(desc(models.Alert.created_at))
                .limit(1)
            )

    def list_alerts(
        self,
        limit: int | None = 100,
        camera_id: str | None = None,
        mode: str | None = None,
        severity: str | None = None,
        alert_date: date | None = None,
        alert_ids: list[int] | None = None,
    ):
        with session_scope() as session:
            priority = case(
                (models.Alert.severity == "critical", 0),
                (models.Alert.severity == "high", 1),
                (models.Alert.severity == "normal", 2),
                else_=3,
            )
            stmt = select(models.Alert, models.Camera.name).join(
                models.Camera, models.Camera.id == models.Alert.camera_id
            ).order_by(priority, desc(models.Alert.created_at))
            if limit is not None:
                stmt = stmt.limit(limit)
            if camera_id:
                stmt = stmt.where(models.Alert.camera_id == camera_id)
            if mode:
                stmt = stmt.where(models.Alert.mode == mode)
            if severity:
                stmt = stmt.where(models.Alert.severity == severity)
            if alert_date:
                zone = ZoneInfo("Asia/Shanghai")
                start = datetime.combine(alert_date, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
                end = (datetime.combine(alert_date, datetime.min.time(), tzinfo=zone) + timedelta(days=1)).astimezone(timezone.utc)
                stmt = stmt.where(models.Alert.created_at >= start, models.Alert.created_at < end)
            if alert_ids is not None:
                stmt = stmt.where(models.Alert.id.in_(alert_ids))
            rows = []
            for alert, camera_name in session.execute(stmt):
                alert.camera_name = camera_name
                rows.append(alert)
            return rows

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
            id_stmt = select(models.Alert.id).where(models.Alert.created_at < cutoff)
            if severity:
                id_stmt = id_stmt.where(models.Alert.severity == severity)
            alert_ids = list(session.scalars(id_stmt))
            if not alert_ids:
                return 0
            session.execute(delete(models.WebhookDelivery).where(models.WebhookDelivery.alert_id.in_(alert_ids)))
            session.execute(delete(models.AlertEvidence).where(models.AlertEvidence.alert_id.in_(alert_ids)))
            stmt = delete(models.Alert).where(models.Alert.id.in_(alert_ids))
            result = session.execute(stmt)
            return result.rowcount or 0
