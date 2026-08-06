from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base, utc_now


class Camera(Base):
    __tablename__ = "cameras"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    scene_type: Mapped[str] = mapped_column(String(40), default="custom", nullable=False, index=True)
    rtsp_url_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    modes_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    geometry_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    schedule_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    options_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    online: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
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
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
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
    shadow: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class TrafficAggregate(Base):
    __tablename__ = "traffic_aggregates"
    __table_args__ = (UniqueConstraint("camera_id", "bucket_start", name="uq_traffic_bucket"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
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
    enhanced_model: Mapped[str] = mapped_column(String(200), default="qwen3.7-plus")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class WebhookSettings(Base):
    __tablename__ = "webhook_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    url: Mapped[str] = mapped_column(Text, default="")
    secret_encrypted: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class DetectorSettings(Base):
    __tablename__ = "detector_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    general_model: Mapped[str] = mapped_column(String(300), default="yolo26n.pt")
    general_device: Mapped[str] = mapped_column(String(50), default="cpu")
    fire_smoke_model: Mapped[str] = mapped_column(String(500), default="models/fire_smoke_yolov8.pt")
    fire_smoke_device: Mapped[str] = mapped_column(String(50), default="cpu")
    model_sha256: Mapped[str] = mapped_column(
        String(64), default="ac0a10257b2bc1f20c9d957f8adeeb61dd6140322fc19d0b4a116cb491776d16"
    )
    license_name: Mapped[str] = mapped_column(String(100), default="AGPL-3.0 (internal pilot only)")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
