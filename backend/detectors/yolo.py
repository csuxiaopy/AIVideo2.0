from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from multiprocessing import get_context

import cv2
import numpy as np

from backend.detectors.black_screen import decode_jpeg
from backend.schemas import Detection


logger = logging.getLogger(__name__)
BYTE_TRACK_LOST_CYCLES = 3

_worker_model = None


def _initialize_inference_worker(threads: int, interop_threads: int) -> None:
    """Give every model process a bounded share of the host CPU."""
    global _worker_model
    _worker_model = None
    os.environ["OMP_NUM_THREADS"] = str(threads)
    os.environ["MKL_NUM_THREADS"] = str(threads)
    os.environ["OPENBLAS_NUM_THREADS"] = str(threads)
    import torch

    cv2.setNumThreads(1)
    torch.set_num_threads(threads)
    torch.set_num_interop_threads(interop_threads)


def _predict_in_worker(
    model_name: str, device: str, imgsz: int, confidence: float, iou: float, jpeg: bytes
) -> tuple[int, int, list[tuple[int, str, float, tuple[float, float, float, float]]]]:
    """Load one model per process and return only serializable raw detections."""
    global _worker_model
    if _worker_model is None:
        from ultralytics import YOLO

        _worker_model = YOLO(model_name)
    image = decode_jpeg(jpeg)
    height, width = image.shape[:2]
    result = _worker_model.predict(
        source=image, imgsz=imgsz, conf=confidence, iou=iou, device=device, verbose=False
    )[0]
    rows = []
    for box in result.boxes:
        class_id = int(box.cls.item())
        x1, y1, x2, y2 = (float(value) for value in box.xyxy[0].tolist())
        rows.append(
            (class_id, str(result.names[class_id]), float(box.conf.item()), (x1 / width, y1 / height, x2 / width, y2 / height))
        )
    return width, height, rows


@dataclass
class Track:
    track_id: int
    center: tuple[float, float]


class CentroidFallbackTracker:
    """Small fallback used when supervision is unavailable; production uses ByteTrack."""

    def __init__(self):
        self.next_id = 1
        self.previous: dict[int, tuple[float, float]] = {}

    def update(self, detections: list[Detection]) -> list[Detection]:
        assigned: set[int] = set()
        for detection in detections:
            x1, y1, x2, y2 = detection.box
            center = ((x1 + x2) / 2, (y1 + y2) / 2)
            candidates = [
                (track_id, (old[0] - center[0]) ** 2 + (old[1] - center[1]) ** 2)
                for track_id, old in self.previous.items() if track_id not in assigned
            ]
            track_id = min(candidates, key=lambda item: item[1])[0] if candidates and min(x[1] for x in candidates) < 0.04 else self.next_id
            if track_id == self.next_id:
                self.next_id += 1
            detection.track_id = track_id
            self.previous[track_id] = center
            assigned.add(track_id)
        self.previous = {track_id: center for track_id, center in self.previous.items() if track_id in assigned}
        return detections


class YoloDetector:
    def __init__(
        self, model_name: str, device: str, imgsz: int, confidence: float, iou: float = 0.5,
        inference_processes: int = 1, threads_per_process: int = 1, interop_threads: int = 1,
    ):
        self.model_name = model_name
        self.device = device
        self.imgsz = imgsz
        self.confidence = confidence
        self.iou = iou
        self.inference_processes = max(1, inference_processes)
        self.threads_per_process = max(1, threads_per_process)
        self.interop_threads = max(1, interop_threads)
        self.executor: ProcessPoolExecutor | None = None
        self.available = False
        self.detail = "YOLO 依赖尚未加载"
        self.trackers: dict[str, object] = {}
        self.load_latency_ms = 0.0
        self.last_latency_ms = 0.0
        self.inference_latencies_ms: deque[float] = deque(maxlen=300)
        started = time.perf_counter()
        try:
            import ultralytics  # noqa: F401

            self.executor = ProcessPoolExecutor(
                max_workers=self.inference_processes,
                mp_context=get_context("spawn"),
                initializer=_initialize_inference_worker,
                initargs=(self.threads_per_process, self.interop_threads),
            )
            self.available = True
            self.detail = (
                f"{model_name} on {device}; {self.inference_processes} processes × "
                f"{self.threads_per_process} threads"
            )
        except Exception as exc:
            self.detail = f"YOLO 不可用：{type(exc).__name__}: {str(exc)[:300]}"
            logger.warning(self.detail)
        finally:
            self.load_latency_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "YOLO model=%s device=%s imgsz=%s conf=%.3f iou=%.3f load_ms=%.1f status=%s",
                model_name, device, imgsz, confidence, iou, self.load_latency_ms,
                "ready" if self.available else "degraded",
            )

    def detect(self, camera_id: str, jpeg: bytes) -> list[Detection]:
        if not self.available or self.executor is None:
            raise RuntimeError(self.detail)
        started = time.perf_counter()
        width, height, rows = self.executor.submit(
            _predict_in_worker,
            self.model_name, self.device, self.imgsz, self.confidence, self.iou, jpeg,
        ).result()
        self.last_latency_ms = (time.perf_counter() - started) * 1000
        self.inference_latencies_ms.append(self.last_latency_ms)
        detections = [
            Detection(class_id=class_id, class_name=class_name, confidence=score, box=box)
            for class_id, class_name, score, box in rows
        ]
        persons = [item for item in detections if item.class_name == "person"]
        self._track(camera_id, persons, width, height)
        return detections

    def status(self) -> dict[str, object]:
        samples = sorted(self.inference_latencies_ms)
        average = sum(samples) / len(samples) if samples else 0.0
        p95 = samples[max(0, int(len(samples) * 0.95) - 1)] if samples else 0.0
        return {
            "status": "ready" if self.available else "degraded",
            "detail": self.detail,
            "model": self.model_name,
            "device": self.device,
            "imgsz": self.imgsz,
            "conf": self.confidence,
            "iou": self.iou,
            "load_latency_ms": round(self.load_latency_ms, 1),
            "latency_ms": round(self.last_latency_ms, 1),
            "average_latency_ms": round(average, 1),
            "p95_latency_ms": round(p95, 1),
            "samples": len(samples),
            "inference_processes": self.inference_processes,
            "threads_per_process": self.threads_per_process,
            "interop_threads": self.interop_threads,
        }

    def close(self) -> None:
        if self.executor is not None:
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.executor = None

    def reset_tracker(self, camera_id: str) -> None:
        self.trackers.pop(camera_id, None)

    def _track(self, camera_id: str, persons: list[Detection], width: int, height: int) -> None:
        if not persons:
            tracker = self.trackers.get(camera_id)
            if isinstance(tracker, CentroidFallbackTracker):
                tracker.update([])
            elif tracker and hasattr(tracker, "update_with_detections"):
                try:
                    import supervision as sv
                    tracker.update_with_detections(sv.Detections.empty())
                except Exception:
                    pass
            return
        tracker = self.trackers.get(camera_id)
        if tracker is None:
            try:
                import supervision as sv

                # supervision scales lost_track_buffer by frame_rate / 30.  Using
                # frame_rate=30 makes this value mean detector updates, not video FPS.
                tracker = sv.ByteTrack(frame_rate=30, lost_track_buffer=BYTE_TRACK_LOST_CYCLES)
            except Exception:
                tracker = CentroidFallbackTracker()
            self.trackers[camera_id] = tracker
        if isinstance(tracker, CentroidFallbackTracker):
            tracker.update(persons)
            return
        try:
            import supervision as sv

            xyxy = np.asarray(
                [
                    [item.box[0] * width, item.box[1] * height, item.box[2] * width, item.box[3] * height]
                    for item in persons
                ],
                dtype=np.float32,
            )
            confidence = np.asarray([item.confidence for item in persons], dtype=np.float32)
            class_id = np.asarray([item.class_id for item in persons], dtype=np.int32)
            tracked = tracker.update_with_detections(
                sv.Detections(xyxy=xyxy, confidence=confidence, class_id=class_id)
            )
            tracked_ids = list(tracked.tracker_id) if tracked.tracker_id is not None else []
            unmatched = set(range(len(persons)))
            for tracked_box, track_id in zip(tracked.xyxy, tracked_ids):
                best = max(
                    unmatched,
                    key=lambda index: self._box_iou(
                        tracked_box,
                        np.asarray([
                            persons[index].box[0] * width, persons[index].box[1] * height,
                            persons[index].box[2] * width, persons[index].box[3] * height,
                        ]),
                    ),
                    default=None,
                )
                if best is not None and self._box_iou(tracked_box, np.asarray([
                    persons[best].box[0] * width, persons[best].box[1] * height,
                    persons[best].box[2] * width, persons[best].box[3] * height,
                ])) > 0:
                    persons[best].track_id = int(track_id)
                    unmatched.remove(best)
        except Exception as exc:
            logger.warning("ByteTrack update failed for %s, using centroid fallback: %s", camera_id, exc)
            fallback = CentroidFallbackTracker()
            self.trackers[camera_id] = fallback
            fallback.update(persons)

    @staticmethod
    def _box_iou(first: np.ndarray, second: np.ndarray) -> float:
        left, top = max(first[0], second[0]), max(first[1], second[1])
        right, bottom = min(first[2], second[2]), min(first[3], second[3])
        intersection = max(0.0, right - left) * max(0.0, bottom - top)
        first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
        second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
        return float(intersection / max(first_area + second_area - intersection, 1e-9))

    @staticmethod
    def people(detections: list[Detection]) -> list[Detection]:
        return [item for item in detections if item.class_name == "person"]

    @staticmethod
    def phone_candidate(detections: list[Detection]) -> bool:
        people = [item for item in detections if item.class_name == "person"]
        phones = [item for item in detections if item.class_name in {"cell phone", "mobile phone"}]
        for person in people:
            px1, py1, px2, py2 = person.box
            margin_x = (px2 - px1) * 0.2
            margin_y = (py2 - py1) * 0.2
            for phone in phones:
                fx1, fy1, fx2, fy2 = phone.box
                cx, cy = (fx1 + fx2) / 2, (fy1 + fy2) / 2
                if px1 - margin_x <= cx <= px2 + margin_x and py1 - margin_y <= cy <= py2 + margin_y:
                    return True
        return False
