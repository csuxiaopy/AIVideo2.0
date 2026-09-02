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
    evidence_dir: Path = ROOT / "data" / "evidence"
    snapshot_dir: Path = ROOT / "data" / "snapshots"
    evidence_retention_days: int = 30
    max_live_previews: int = 4
    live_preview_fps: float = 2.0
    live_preview_timeout_seconds: int = 60
    frame_capture_timeout_seconds: int = 15
    yolo_model_path: str = "models/yolo26s.pt"
    yolo_device: str = "cpu"
    yolo_imgsz: int = 640
    yolo_confidence: float = 0.35
    yolo_iou: float = 0.5
    yolo_inference_timeout_seconds: int = 30
    analysis_queue_maxsize: int = 256
    fire_smoke_model: str = "models/fire_smoke_yolov8.pt"
    fire_smoke_sha256: str = "ac0a10257b2bc1f20c9d957f8adeeb61dd6140322fc19d0b4a116cb491776d16"
    fire_smoke_device: str = "cpu"
    fire_smoke_imgsz: int = 640
    scheduler_enabled: bool = True
    analysis_workers: int = 2
    fire_smoke_workers: int = 1
    web_dist_dir: Path = ROOT / "frontend" / "dist"

    def prepare(self) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.prepare()
    return settings
