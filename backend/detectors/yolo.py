from __future__ import annotations

import logging
import os
import queue
import threading
import time
import zlib
from collections import defaultdict
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from multiprocessing import get_context

import cv2
import numpy as np
from prometheus_client import Counter, Histogram

from backend.detectors.black_screen import decode_jpeg
from backend.schemas import Detection


logger = logging.getLogger(__name__)
BYTE_TRACK_LOST_CYCLES = 3
YOLO_BATCHES = Counter("monitor_yolo_batches_total", "Completed general YOLO batches")
YOLO_BATCH_SIZE = Histogram(
    "monitor_yolo_batch_size", "Actual general YOLO batch size",
    buckets=(1, 2, 4, 8, 12, 16, 24, 32, 48, 64),
)
YOLO_BATCH_COLLECTION = Histogram(
    "monitor_yolo_batch_collection_seconds", "Time spent collecting a general YOLO batch"
)
YOLO_BATCH_INFERENCE = Histogram(
    "monitor_yolo_batch_inference_seconds", "General YOLO batch inference round-trip time"
)

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


def _predict_batch_in_worker(
    model_name: str, device: str, imgsz: int, confidence: float, iou: float, jpegs: list[bytes]
) -> list[tuple[int, int, list[tuple[int, str, float, tuple[float, float, float, float]]]]]:
    global _worker_model
    if _worker_model is None:
        from ultralytics import YOLO

        _worker_model = YOLO(model_name)
    images = [decode_jpeg(jpeg) for jpeg in jpegs]
    predictions = _worker_model.predict(
        source=images, imgsz=imgsz, conf=confidence, iou=iou, device=device, verbose=False
    )
    output = []
    for image, result in zip(images, predictions, strict=True):
        height, width = image.shape[:2]
        rows = []
        for box in result.boxes:
            class_id = int(box.cls.item())
            x1, y1, x2, y2 = (float(value) for value in box.xyxy[0].tolist())
            rows.append((
                class_id, str(result.names[class_id]), float(box.conf.item()),
                (x1 / width, y1 / height, x2 / width, y2 / height),
            ))
        output.append((width, height, rows))
    return output


@dataclass
class BatchRequest:
    jpeg: bytes
    ready: threading.Event
    result: tuple | None = None
    error: BaseException | None = None


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
        batch_size: int = 1, batch_wait_ms: float = 10.0,
    ):
        self.model_name = model_name
        self.device = device
        self.devices = [item.strip() for item in device.split(",") if item.strip()]
        if not self.devices:
            raise ValueError("At least one YOLO device is required")
        self.imgsz = imgsz
        self.confidence = confidence
        self.iou = iou
        self.inference_processes = max(1, inference_processes)
        self.threads_per_process = max(1, threads_per_process)
        self.interop_threads = max(1, interop_threads)
        self.batch_size = max(1, min(batch_size, 64))
        self.batch_wait_ms = max(0.0, min(float(batch_wait_ms), 1000.0))
        self.executors: list[ProcessPoolExecutor] = []
        self.batch_queues: list[queue.Queue[BatchRequest | None]] = []
        self.batch_threads: list[threading.Thread] = []
        self.available = False
        self.detail = "YOLO 依赖尚未加载"
        self.trackers: dict[str, object] = {}
        self.load_latency_ms = 0.0
        self.last_latency_ms = 0.0
        self.inference_latencies_ms: deque[float] = deque(maxlen=300)
        self.batch_sizes: deque[int] = deque(maxlen=300)
        self.batch_collection_ms: deque[float] = deque(maxlen=300)
        self.batch_inference_ms: deque[float] = deque(maxlen=300)
        self.batch_count = 0
        self.full_batch_count = 0
        started = time.perf_counter()
        try:
            import ultralytics  # noqa: F401

            for inference_device in self.devices:
                executor = ProcessPoolExecutor(
                    max_workers=self.inference_processes,
                    mp_context=get_context("spawn"),
                    initializer=_initialize_inference_worker,
                    initargs=(self.threads_per_process, self.interop_threads),
                )
                self.executors.append(executor)
                batch_queue: queue.Queue[BatchRequest | None] = queue.Queue(maxsize=4096)
                self.batch_queues.append(batch_queue)
                if self.batch_size > 1:
                    thread = threading.Thread(
                        target=self._batch_loop,
                        args=(inference_device, executor, batch_queue),
                        name=f"yolo-gpu-batcher-{inference_device}", daemon=True,
                    )
                    thread.start()
                    self.batch_threads.append(thread)
            self.available = True
            self.detail = (
                f"{model_name} on {','.join(self.devices)}; "
                f"{len(self.devices)} devices × {self.inference_processes} processes × "
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

    def detect_latest(self, camera_id: str, source):
        selected = []
        def choose():
            frame = source()
            if frame is None:
                raise RuntimeError("Latest frame unavailable at GPU dispatch")
            selected.append(frame)
            return frame.jpeg
        detections = self.detect(camera_id, choose)
        return detections, selected[0]

    def _device_index(self, camera_id: str) -> int:
        return zlib.crc32(camera_id.encode("utf-8")) % len(self.devices)

    def detect(self, camera_id: str, jpeg: bytes) -> list[Detection]:
        if not self.available or not self.executors:
            raise RuntimeError(self.detail)
        started = time.perf_counter()
        device_index = self._device_index(camera_id)
        if self.batch_size > 1:
            request = BatchRequest(jpeg=jpeg, ready=threading.Event())
            self.batch_queues[device_index].put(request)
            request.ready.wait()
            if request.error:
                raise request.error
            width, height, rows = request.result
        else:
            jpeg = jpeg() if callable(jpeg) else jpeg
            width, height, rows = self.executors[device_index].submit(
                _predict_in_worker,
                self.model_name, self.devices[device_index], self.imgsz,
                self.confidence, self.iou, jpeg,
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

    def _batch_loop(
        self, device: str, executor: ProcessPoolExecutor,
        batch_queue: queue.Queue[BatchRequest | None],
    ) -> None:
        while True:
            first = batch_queue.get()
            if first is None:
                return
            collection_started = time.perf_counter()
            requests = [first]
            deadline = time.monotonic() + self.batch_wait_ms / 1000
            while len(requests) < self.batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = batch_queue.get(timeout=remaining)
                except queue.Empty:
                    break
                if item is None:
                    batch_queue.put(None)
                    break
                requests.append(item)
            try:
                collection_ms = (time.perf_counter() - collection_started) * 1000
                inference_started = time.perf_counter()
                results = executor.submit(
                    _predict_batch_in_worker, self.model_name, device, self.imgsz,
                    self.confidence, self.iou, [request.jpeg() if callable(request.jpeg) else request.jpeg for request in requests],
                ).result()
                inference_ms = (time.perf_counter() - inference_started) * 1000
                actual_size = len(requests)
                self.batch_count += 1
                self.full_batch_count += int(actual_size == self.batch_size)
                self.batch_sizes.append(actual_size)
                self.batch_collection_ms.append(collection_ms)
                self.batch_inference_ms.append(inference_ms)
                YOLO_BATCHES.inc()
                YOLO_BATCH_SIZE.observe(actual_size)
                YOLO_BATCH_COLLECTION.observe(collection_ms / 1000)
                YOLO_BATCH_INFERENCE.observe(inference_ms / 1000)
                for request, result in zip(requests, results, strict=True):
                    request.result = result
            except BaseException as exc:
                for request in requests:
                    request.error = exc
            finally:
                for request in requests:
                    request.ready.set()

    def status(self) -> dict[str, object]:
        samples = sorted(self.inference_latencies_ms)
        average = sum(samples) / len(samples) if samples else 0.0
        p95 = samples[max(0, int(len(samples) * 0.95) - 1)] if samples else 0.0
        batch_sizes = sorted(self.batch_sizes)
        collection = sorted(self.batch_collection_ms)
        batch_inference = sorted(self.batch_inference_ms)

        def percentile(values, ratio):
            return values[max(0, int(len(values) * ratio) - 1)] if values else 0.0

        return {
            "status": "ready" if self.available else "degraded",
            "detail": self.detail,
            "model": self.model_name,
            "device": self.device,
            "devices": self.devices,
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
            "batch_size": self.batch_size,
            "batch_wait_ms": self.batch_wait_ms,
            "batch_metrics": {
                "count": self.batch_count,
                "last_size": self.batch_sizes[-1] if self.batch_sizes else 0,
                "average_size": round(sum(batch_sizes) / len(batch_sizes), 2) if batch_sizes else 0.0,
                "p50_size": percentile(batch_sizes, 0.50),
                "p95_size": percentile(batch_sizes, 0.95),
                "full_batch_ratio": round(self.full_batch_count / self.batch_count, 4) if self.batch_count else 0.0,
                "collection_average_ms": round(sum(collection) / len(collection), 2) if collection else 0.0,
                "collection_p95_ms": round(percentile(collection, 0.95), 2),
                "inference_average_ms": round(sum(batch_inference) / len(batch_inference), 2) if batch_inference else 0.0,
                "inference_p95_ms": round(percentile(batch_inference, 0.95), 2),
            },
        }

    def close(self) -> None:
        for batch_queue in self.batch_queues:
            batch_queue.put(None)
        for thread in self.batch_threads:
            thread.join(timeout=5)
        self.batch_threads.clear()
        for executor in self.executors:
            executor.shutdown(wait=False, cancel_futures=True)
        self.executors.clear()

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
