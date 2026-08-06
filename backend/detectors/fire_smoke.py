from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

from backend.detectors.black_screen import decode_jpeg
from backend.schemas import Detection


logger = logging.getLogger(__name__)


class FireSmokeDetector:
    """Dedicated local detector. It never downloads weights at service startup."""

    def __init__(
        self, model_path: str, device: str = "cpu", imgsz: int = 640, expected_sha256: str = ""
    ):
        self.model_path = model_path
        self.device = device
        self.imgsz = imgsz
        self.model = None
        self.available = False
        self.detail = "烟火模型未配置"
        self.model_hash = ""
        self.expected_sha256 = expected_sha256.lower()
        self.version = Path(model_path).name
        self.last_latency_ms = 0
        self.processed = 0
        self.failures = 0
        self._load()

    def _load(self) -> None:
        path = Path(self.model_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_file():
            self.detail = f"烟火模型文件不存在: {path}"
            return
        try:
            from ultralytics import YOLO

            self.model_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if self.expected_sha256 and self.model_hash != self.expected_sha256:
                raise RuntimeError(
                    f"model SHA256 mismatch: expected {self.expected_sha256}, got {self.model_hash}"
                )
            self.model = YOLO(str(path))
            names = {str(value).lower() for value in self.model.names.values()}
            if not ({"fire", "flame"} & names) or "smoke" not in names:
                raise RuntimeError(f"模型类别必须包含 fire/flame 和 smoke，实际为 {sorted(names)}")
            self.available = True
            self.detail = f"{path.name} on {self.device}"
        except Exception as exc:
            self.failures += 1
            self.detail = f"烟火模型不可用: {type(exc).__name__}: {str(exc)[:300]}"
            logger.warning(self.detail)

    def detect(self, jpeg: bytes) -> list[Detection]:
        if not self.available or self.model is None:
            raise RuntimeError(self.detail)
        started = time.perf_counter()
        try:
            image = decode_jpeg(jpeg)
            height, width = image.shape[:2]
            result = self.model.predict(
                source=image, imgsz=self.imgsz, conf=0.20, device=self.device, verbose=False
            )[0]
            detections: list[Detection] = []
            for box in result.boxes:
                class_id = int(box.cls.item())
                class_name = str(result.names[class_id]).lower()
                if class_name == "flame":
                    class_name = "fire"
                if class_name not in {"fire", "smoke"}:
                    continue
                x1, y1, x2, y2 = (float(value) for value in box.xyxy[0].tolist())
                detections.append(
                    Detection(
                        class_id=class_id,
                        class_name=class_name,
                        confidence=float(box.conf.item()),
                        box=(x1 / width, y1 / height, x2 / width, y2 / height),
                    )
                )
            self.processed += 1
            return detections
        except Exception:
            self.failures += 1
            raise
        finally:
            self.last_latency_ms = round((time.perf_counter() - started) * 1000)

    def status(self) -> dict[str, object]:
        return {
            "status": "ready" if self.available else "not_configured",
            "detail": self.detail,
            "model": self.version,
            "sha256": self.model_hash,
            "device": self.device,
            "latency_ms": self.last_latency_ms,
            "processed": self.processed,
            "failures": self.failures,
            "license": "AGPL-3.0 (internal pilot only)",
        }
