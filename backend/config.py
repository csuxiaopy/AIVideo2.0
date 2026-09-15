from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_host: str = "127.0.0.1"
    app_port: int = 8100
    app_reload: bool = False
    database_url: str = "postgresql+psycopg://monitor:monitor_pass@127.0.0.1:5432/monitor"
    redis_url: str = "redis://127.0.0.1:6379/0"
    app_encryption_key: str = "development-only-change-me"
    admin_username: str = ""
    admin_display_name: str = "系统管理员"
    admin_password: str = ""
    session_idle_hours: int = 8
    secure_cookies: bool = False
    allowed_origins: str = "http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:5174,http://localhost:5174"
    evidence_dir: Path = ROOT / "data" / "evidence"
    snapshot_dir: Path = ROOT / "data" / "snapshots"
    evidence_retention_days: int = 30
    max_live_previews: int = 4
    live_preview_fps: float = 2.0
    live_preview_timeout_seconds: int = 60
    frame_capture_timeout_seconds: int = 15
    capture_fps: float = 1.0
    capture_max_height: int = 960
    capture_decode_devices: str = ""
    capture_gpu_streams_per_device: int = 0
    capture_cpu_camera_ids: str = ""
    yolo_model_path: str = "models/yolo26s.pt"
    yolo_device: str = "cpu"
    yolo_imgsz: int = 640
    yolo_confidence: float = 0.35
    yolo_iou: float = 0.5
    yolo_inference_timeout_seconds: int = 30
    yolo_inference_processes: int = 4
    yolo_batch_size: int = 1
    yolo_batch_wait_ms: float = 10.0
    yolo_threads_per_process: int = 5
    yolo_interop_threads: int = 1
    analysis_queue_maxsize: int = 256
    # Separate staged switch: this does NOT enable asynchronous review/alerts.
    async_capture_persistence: bool = False
    background_io_workers: int = 4
    async_postprocessing: bool = False
    background_queue_capacity: int = 4096
    review_workers: int = 2
    review_timeout_seconds: float = 90
    review_max_evidence_age_seconds: float = 120
    notification_workers: int = 4
    fire_smoke_model: str = "models/fire_smoke_yolov8.pt"
    fire_smoke_sha256: str = "ac0a10257b2bc1f20c9d957f8adeeb61dd6140322fc19d0b4a116cb491776d16"
    fire_smoke_device: str = "cpu"
    fire_smoke_imgsz: int = 640
    scheduler_enabled: bool = True
    analysis_workers: int = 10
    fire_smoke_workers: int = 1
    fire_smoke_min_interval_seconds: float = 30.0
    web_dist_dir: Path = ROOT / "frontend" / "dist"

    def prepare(self) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.prepare()
    return settings
