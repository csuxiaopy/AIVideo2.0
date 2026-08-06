from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


class Mode(str, Enum):
    BLACK_SCREEN = "black_screen"
    OFF_DUTY = "off_duty"
    ON_DUTY = "on_duty"
    PEOPLE_FLOW = "people_flow"
    PHONE_USE = "phone_use"
    SMOKING = "smoking"
    FIRE_SMOKE = "fire_smoke"
    INTRUSION = "intrusion"


class SceneType(str, Enum):
    WORKSTATION = "workstation"
    CUSTOMER_AREA = "customer_area"
    SECURITY_AREA = "security_area"
    CUSTOM = "custom"


ALL_MODES = {mode.value for mode in Mode}
Point = tuple[Annotated[float, Field(ge=0, le=1)], Annotated[float, Field(ge=0, le=1)]]


class CameraOptions(BaseModel):
    health_interval_seconds: int = Field(default=5, ge=2, le=60)
    yolo_fps: float = Field(default=1.0, ge=0.1, le=10)
    behavior_interval_seconds: int = Field(default=15, ge=5, le=300)
    off_duty_seconds: int = Field(default=300, ge=30, le=86400)
    shift_grace_seconds: int = Field(default=60, ge=0, le=3600)
    alert_cooldown_seconds: int = Field(default=300, ge=0, le=86400)
    black_mean_max: float = Field(default=18.0, ge=0, le=255)
    black_std_max: float = Field(default=12.0, ge=0, le=255)
    black_ratio_min: float = Field(default=0.92, ge=0, le=1)
    fire_smoke_fps: float = Field(default=1.0, ge=0.2, le=5)
    fire_confidence: float = Field(default=0.55, ge=0, le=1)
    smoke_confidence: float = Field(default=0.45, ge=0, le=1)
    intrusion_confidence: float = Field(default=0.50, ge=0, le=1)
    intrusion_cooldown_seconds: int = Field(default=60, ge=0, le=86400)


class NamedPolygon(BaseModel):
    name: str = Field(default="禁区", min_length=1, max_length=100)
    points: list[Point] = Field(min_length=3, max_length=50)


class GeometrySpec(BaseModel):
    post_roi: list[Point] = Field(default_factory=list, max_length=50)
    flow_line: list[Point] = Field(default_factory=list, max_length=2)
    intrusion_zone: NamedPolygon | None = None

    @field_validator("post_roi")
    @classmethod
    def valid_roi(cls, value: list[Point]) -> list[Point]:
        if value and len(value) < 3:
            raise ValueError("岗位区域至少需要3个点")
        return value

    @field_validator("flow_line")
    @classmethod
    def valid_line(cls, value: list[Point]) -> list[Point]:
        if value and len(value) != 2:
            raise ValueError("人流统计线必须包含2个点")
        return value


class Shift(BaseModel):
    start: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class ScheduleSpec(BaseModel):
    timezone: str = "Asia/Shanghai"
    weekly: dict[str, list[Shift]] = Field(default_factory=dict)
    holidays: list[str] = Field(default_factory=list)

    @field_validator("weekly")
    @classmethod
    def valid_days(cls, value: dict[str, list[Shift]]) -> dict[str, list[Shift]]:
        allowed = {str(day) for day in range(7)}
        if set(value) - allowed:
            raise ValueError("排班星期必须使用0到6，0代表周一")
        return value


class CameraCreate(BaseModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=200)
    rtsp_url: str = Field(min_length=1, max_length=2000)
    enabled: bool = True
    scene_type: SceneType = SceneType.CUSTOM
    modes: list[Mode] = Field(min_length=1, max_length=len(Mode))
    geometry: GeometrySpec = Field(default_factory=GeometrySpec)
    schedule: ScheduleSpec = Field(default_factory=ScheduleSpec)
    options: CameraOptions = Field(default_factory=CameraOptions)

    @field_validator("rtsp_url")
    @classmethod
    def valid_source(cls, value: str) -> str:
        if not value.startswith(("rtsp://", "rtsps://", "file://")):
            raise ValueError("视频源必须是 rtsp://、rtsps:// 或 file://")
        return value

    @field_validator("modes")
    @classmethod
    def unique_modes(cls, value: list[Mode]) -> list[Mode]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def validate_mode_dependencies(self):
        selected = set(self.modes)
        if selected & {Mode.OFF_DUTY, Mode.ON_DUTY, Mode.PHONE_USE} and len(self.geometry.post_roi) < 3:
            raise ValueError("在岗或离岗模式必须配置岗位区域")
        if Mode.PEOPLE_FLOW in selected and len(self.geometry.flow_line) != 2:
            raise ValueError("人流模式必须配置统计线")
        if Mode.INTRUSION in selected and self.geometry.intrusion_zone is None:
            raise ValueError("区域入侵模式必须配置禁区")
        return self


class CameraPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    rtsp_url: str | None = Field(default=None, min_length=1, max_length=2000)
    enabled: bool | None = None
    scene_type: SceneType | None = None
    options: CameraOptions | None = None

    @field_validator("rtsp_url")
    @classmethod
    def valid_source(cls, value: str | None) -> str | None:
        if value and not value.startswith(("rtsp://", "rtsps://", "file://")):
            raise ValueError("视频源必须是 rtsp://、rtsps:// 或 file://")
        return value


class ModesUpdate(BaseModel):
    modes: list[Mode] = Field(min_length=1, max_length=len(Mode))

    @field_validator("modes")
    @classmethod
    def unique_modes(cls, value: list[Mode]) -> list[Mode]:
        return list(dict.fromkeys(value))


class ModelSettingsUpdate(BaseModel):
    provider: str = Field(default="openai_compatible", pattern=r"^(openai_compatible|mock)$")
    base_url: str = ""
    api_key: str = Field(default="", max_length=1000)
    economy_model: str = Field(min_length=1, max_length=200)
    enhanced_model: str = Field(min_length=1, max_length=200)

    @field_validator("base_url")
    @classmethod
    def secure_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if value and not value.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError("模型 Base URL 必须使用 HTTPS，本机地址除外")
        return value


class WebhookSettingsUpdate(BaseModel):
    enabled: bool = False
    url: str = ""
    secret: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_enabled(self):
        if self.enabled and not self.url.startswith("https://"):
            raise ValueError("启用 Webhook 时必须填写 HTTPS URL 和签名密钥")
        return self


class VLMResult(BaseModel):
    mode: Mode
    status: str = Field(pattern=r"^(confirmed|suspected|uncertain|none)$")
    confidence: float = Field(ge=0, le=1)
    evidence_frames: list[int] = Field(default_factory=list)
    reason: str = Field(default="", max_length=1000)
    need_review: bool = False


class Detection(BaseModel):
    class_id: int
    class_name: str
    confidence: float
    box: tuple[float, float, float, float]
    track_id: int | None = None


class WorkerStatus(BaseModel):
    name: str
    status: str
    detail: str = ""
    last_heartbeat: datetime
    processed: int = 0
    failures: int = 0


class DetectorSettingsUpdate(BaseModel):
    general_model: str = Field(min_length=1, max_length=300)
    general_device: str = Field(default="cpu", min_length=1, max_length=50)
    fire_smoke_model: str = Field(min_length=1, max_length=500)
    fire_smoke_device: str = Field(default="cpu", min_length=1, max_length=50)
    model_sha256: str = Field(default="", pattern=r"^(?:[0-9a-fA-F]{64})?$")
    license_name: str = Field(default="AGPL-3.0 (internal pilot only)", max_length=100)
