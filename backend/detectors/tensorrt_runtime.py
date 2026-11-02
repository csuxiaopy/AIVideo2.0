"""Verified optional TensorRT engines with per-process PyTorch fallback."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import nullcontext
import threading
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class DetectionModel:
    def __init__(self, weights: str, device: str, imgsz: int = 640):
        from ultralytics import YOLO
        self.weights, self.device, self.imgsz = weights, device, imgsz
        self.lock = threading.RLock()
        self.backend = "pytorch"
        self.model = None
        path = Path(weights)
        engine = path.with_suffix(".fp16.engine")
        manifest = engine.with_suffix(".json")
        allowlist = {name.strip() for name in os.getenv("TENSORRT_MODEL_ALLOWLIST", "").split(",") if name.strip()}
        enabled = os.getenv("TENSORRT_ENABLED", "false").lower() == "true"
        if enabled and str(device) != "cpu" and (not allowlist or path.name in allowlist):
            try:
                import tensorrt
                meta = json.loads(manifest.read_text())
                if meta["source_sha256"] != hashlib.sha256(path.read_bytes()).hexdigest():
                    raise ValueError("engine source weight checksum mismatch")
                if meta["engine_sha256"] != hashlib.sha256(engine.read_bytes()).hexdigest():
                    raise ValueError("engine checksum mismatch")
                if meta["tensorrt"] != tensorrt.__version__ or meta["imgsz"] != imgsz:
                    raise ValueError("engine runtime or image size mismatch")
                candidate = YOLO(str(engine), task="detect")
                with self._device_context():
                    candidate.predict(source=np.zeros((imgsz, imgsz, 3), dtype=np.uint8), device=device, imgsz=imgsz, verbose=False)
                backend = candidate.predictor.model
                if getattr(backend, "format", None) != "engine" and not getattr(backend, "engine", False):
                    raise RuntimeError("model did not load the TensorRT backend")
                self.model, self.backend = candidate, "tensorrt"
                logger.info("TensorRT engine active: %s device=%s", engine, device)
            except Exception:
                logger.exception("TensorRT initialization failed; using PyTorch: %s", weights)
        if self.model is None:
            self.model = YOLO(weights)

    @property
    def names(self):
        return self.model.names

    def _device_context(self):
        if str(self.device) == "cpu":
            return nullcontext()
        import torch
        device = int(self.device) if str(self.device).isdigit() else self.device
        return torch.cuda.device(device)

    def predict(self, **kwargs):
        # CUDA's current device is thread-local. Startup and inference may run
        # on different threads, especially for the dedicated fire detector.
        with self.lock, self._device_context():
            try:
                return self.model.predict(**kwargs)
            except Exception:
                if self.backend != "tensorrt":
                    raise
                logger.exception("TensorRT inference failed; retrying original weights: %s", self.weights)
                from ultralytics import YOLO
                self.model = YOLO(self.weights)
                self.backend = "pytorch"
                return self.model.predict(**kwargs)
