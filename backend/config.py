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
    database_url: str = f"sqlite:///{(ROOT / 'data' / 'yolo_vlm.db').as_posix()}"
    redis_url: str = "redis://127.0.0.1:6379/0"
    app_encryption_key: str = "development-only-change-me"
    evidence_dir: Path = ROOT / "data" / "evidence"
    snapshot_dir: Path = ROOT / "data" / "snapshots"
    evidence_retention_days: int = 30
    max_live_previews: int = 4
    live_preview_fps: float = 2.0
    live_preview_timeout_seconds: int = 60
    frame_capture_timeout_seconds: int = 15
    # 通用检测模型（YOLO26s 或自训练 best.pt）。路径相对项目根目录，
    # 切换模型只需修改此处 / .env 的 YOLO_MODEL，无需改业务代码。
    yolo_model: str = "models/yolo26s.pt"
    yolo_device: str = "cpu"
    yolo_imgsz: int = 640
    yolo_confidence: float = 0.35
    yolo_iou: float = 0.5
    # 同一时刻最多并发执行的 YOLO 推理数（防止多 worker 同时压同一模型导致 CPU 过载）
    yolo_max_concurrency: int = 2
    fire_smoke_model: str = "models/fire_smoke_yolov8.pt"
    fire_smoke_sha256: str = "ac0a10257b2bc1f20c9d957f8adeeb61dd6140322fc19d0b4a116cb491776d16"
    fire_smoke_device: str = "cpu"
    fire_smoke_imgsz: int = 640
    scheduler_enabled: bool = True
    # 96 路摄像头（每路 60s 一帧 ≈ 1.6 张/秒）时建议 >= 8；小规模部署可调小
    analysis_workers: int = 8
    fire_smoke_workers: int = 1
    shadow_mode: bool = True
    web_dist_dir: Path = ROOT / "frontend" / "dist"

    def prepare(self) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite"):
            database_path = self.database_url.split("///", 1)[-1]
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.prepare()
    return settings
