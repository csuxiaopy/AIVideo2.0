from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.database import Base, utc_now


class Camera(Base):
    __tablename__ = "cameras"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    scene_type: Mapped[str] = mapped_column(String(40), default="workstation", nullable=False, index=True)
    rtsp_url_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    modes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    geometry_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    schedule_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    intrusion_schedule_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    directory_id: Mapped[int | None] = mapped_column(
        ForeignKey("camera_directories.id", ondelete="SET NULL"), index=True
    )
    options_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    frame_interval_seconds: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    online: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_frame_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_analysis_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[str | None] = mapped_column(
        ForeignKey("cameras.id", ondelete="SET NULL", onupdate="CASCADE"), index=True
    )
    camera_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    mode: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence_path: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(200))
    provider: Mapped[str | None] = mapped_column(String(100))
    model: Mapped[str | None] = mapped_column(String(200))
    severity: Mapped[str] = mapped_column(String(20), default="info", nullable=False, index=True)
    zone_name: Mapped[str | None] = mapped_column(String(200))
    local_model: Mapped[str | None] = mapped_column(String(300))
    model_version: Mapped[str | None] = mapped_column(String(100))
    usage_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE", onupdate="CASCADE"), index=True)
    directory_id: Mapped[int | None] = mapped_column(Integer, index=True)
    alert_name: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("analyses.id", ondelete="SET NULL"))
    mode: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(30), default="confirmed")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    severity: Mapped[str] = mapped_column(String(20), default="normal", nullable=False, index=True)
    zone_name: Mapped[str | None] = mapped_column(String(200))
    local_model: Mapped[str | None] = mapped_column(String(300))
    model_version: Mapped[str | None] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence_path: Mapped[str | None] = mapped_column(Text)
    webhook_status: Mapped[str] = mapped_column(String(30), default="pending")
    shadow: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    event_phase: Mapped[str | None] = mapped_column(String(30))
    event_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    evidences: Mapped[list["AlertEvidence"]] = relationship(
        back_populates="alert", cascade="all, delete-orphan", lazy="selectin",
        order_by="AlertEvidence.sort_order",
    )


class AlertEvidence(Base):
    __tablename__ = "alert_evidences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[int] = mapped_column(
        ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    camera_id: Mapped[str] = mapped_column(String(100), nullable=False)
    camera_name: Mapped[str] = mapped_column(String(200), nullable=False)
    evidence_path: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    alert: Mapped["Alert"] = relationship(back_populates="evidences")


class CaptchaChallenge(Base):
    __tablename__ = "captcha_challenges"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    answer_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class TrafficAggregate(Base):
    __tablename__ = "traffic_aggregates"
    __table_args__ = (UniqueConstraint("camera_id", "bucket_start", name="uq_traffic_bucket"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE", onupdate="CASCADE"), index=True)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    current_count: Mapped[int] = mapped_column(Integer, default=0)
    entered: Mapped[int] = mapped_column(Integer, default=0)
    exited: Mapped[int] = mapped_column(Integer, default=0)


class ModelSettings(Base):
    __tablename__ = "model_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    provider: Mapped[str] = mapped_column(String(100), default="openai_compatible")
    base_url: Mapped[str] = mapped_column(Text, default="")
    api_key_encrypted: Mapped[str] = mapped_column(Text, default="")
    economy_model: Mapped[str] = mapped_column(String(200), default="qwen3.7-flash")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class WebhookSettings(Base):
    __tablename__ = "webhook_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    url: Mapped[str] = mapped_column(Text, default="")
    secret_encrypted: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class CameraDirectory(Base):
    __tablename__ = "camera_directories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class WebhookTarget(Base):
    __tablename__ = "webhook_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    secret_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)
    auto_severities_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (UniqueConstraint("alert_id", "webhook_target_id", name="uq_alert_webhook_target"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False, index=True)
    webhook_target_id: Mapped[int | None] = mapped_column(ForeignKey("webhook_targets.id", ondelete="SET NULL"), index=True)
    target_name: Mapped[str] = mapped_column(String(200), nullable=False)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class DetectorSettings(Base):
    __tablename__ = "detector_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    general_model: Mapped[str] = mapped_column(String(300), default="yolo26s.pt")
    general_device: Mapped[str] = mapped_column(String(50), default="cpu")
    fire_smoke_model: Mapped[str] = mapped_column(String(500), default="models/fire_smoke_yolov8.pt")
    fire_smoke_device: Mapped[str] = mapped_column(String(50), default="cpu")
    model_sha256: Mapped[str] = mapped_column(
        String(64), default="ac0a10257b2bc1f20c9d957f8adeeb61dd6140322fc19d0b4a116cb491776d16"
    )
    license_name: Mapped[str] = mapped_column(String(100), default="AGPL-3.0 (internal pilot only)")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class RetentionSettings(Base):
    __tablename__ = "retention_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    alert_retention_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    log_retention_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    auto_cleanup_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class DisplaySettings(Base):
    __tablename__ = "display_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    show_traffic_report: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    show_current_store_count: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user", index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_username: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    actor_display_name: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(80), default="", nullable=False, index=True)
    target_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    summary_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    ip_address: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    user_agent: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )


class ModelCallLog(Base):
    __tablename__ = "model_call_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[str | None] = mapped_column(
        ForeignKey("cameras.id", ondelete="SET NULL", onupdate="CASCADE"), nullable=True, index=True
    )
    camera_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    modes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    stage: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    model: Mapped[str] = mapped_column(String(200), default="", nullable=False, index=True)
    request_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    usage_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    raw_response: Mapped[str] = mapped_column(Text, default="", nullable=False)
    parsed_response_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )
