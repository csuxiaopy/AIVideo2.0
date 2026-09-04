from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Callable

from backend.security import redact_rtsp
from backend.schemas import Detection


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FramePacket:
    captured_at: datetime
    jpeg: bytes
    sequence: int


@dataclass(frozen=True)
class PersonOverlay:
    box: tuple[float, float, float, float]
    confidence: float
    track_id: int | None


@dataclass(frozen=True)
class SafetyOverlay:
    kind: str
    box: tuple[float, float, float, float]
    confidence: float


@dataclass(frozen=True)
class ObjectOverlay:
    kind: str
    box: tuple[float, float, float, float]
    confidence: float


PHONE_CLASSES = {"cell phone", "mobile phone"}


def phone_overlays(detections: list[Detection]) -> list[ObjectOverlay]:
    return [
        ObjectOverlay("phone", item.box, item.confidence)
        for item in detections
        if item.class_name.lower() in PHONE_CLASSES
    ]


def draw_object_overlays(jpeg: bytes, overlays: list[ObjectOverlay]) -> bytes:
    """Draw preview-only generic object boxes without changing analysis frames."""
    if not overlays:
        return jpeg
    try:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return jpeg
        height, width = image.shape[:2]
        thickness = max(2, round(min(width, height) / 260))
        font_scale = max(0.45, min(width, height) / 900)
        color = (0, 205, 255)  # BGR amber/cyan, distinct from person green.
        text_color = (8, 23, 16)

        for overlay in overlays:
            x1, y1, x2, y2 = overlay.box
            left = max(0, min(width - 1, round(x1 * width)))
            top = max(0, min(height - 1, round(y1 * height)))
            right = max(left + 1, min(width - 1, round(x2 * width)))
            bottom = max(top + 1, min(height - 1, round(y2 * height)))
            cv2.rectangle(image, (left, top), (right, bottom), color, thickness, cv2.LINE_AA)
            label = f"PHONE {overlay.confidence:.0%}"
            (text_width, text_height), baseline = cv2.getTextSize(
                label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness
            )
            label_left = min(left, max(0, width - text_width - 12))
            label_top = max(0, top - text_height - baseline - 8)
            label_right = min(width - 1, label_left + text_width + 12)
            cv2.rectangle(image, (label_left, label_top), (label_right, top), color, -1)
            cv2.putText(
                image, label, (label_left + 6, max(text_height + 1, top - baseline - 4)),
                cv2.FONT_HERSHEY_SIMPLEX, font_scale, text_color, thickness, cv2.LINE_AA,
            )

        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])
        return encoded.tobytes() if ok else jpeg
    except Exception:
        logger.exception("Failed to draw object overlays")
        return jpeg


def draw_person_overlays(
    jpeg: bytes,
    overlays: list[PersonOverlay],
    flow_states: dict[int, object] | None = None,
    flow_summary: tuple[int, int, int] | None = None,
) -> bytes:
    """Draw preview-only person boxes while leaving the analysis frame untouched."""
    if not overlays:
        return jpeg
    try:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return jpeg
        height, width = image.shape[:2]
        thickness = max(2, round(min(width, height) / 260))
        font_scale = max(0.45, min(width, height) / 900)
        color = (117, 229, 71)
        text_color = (8, 23, 16)

        for overlay in overlays:
            x1, y1, x2, y2 = overlay.box
            left = max(0, min(width - 1, round(x1 * width)))
            top = max(0, min(height - 1, round(y1 * height)))
            right = max(left + 1, min(width - 1, round(x2 * width)))
            bottom = max(top + 1, min(height - 1, round(y2 * height)))
            cv2.rectangle(image, (left, top), (right, bottom), color, thickness, cv2.LINE_AA)

            person_id = overlay.track_id if overlay.track_id is not None else "-"
            state = (flow_states or {}).get(overlay.track_id) if overlay.track_id is not None else None
            label = f"P#{person_id}  {overlay.confidence:.0%}"
            if state is not None:
                label = f"P#{person_id} {state.first_zone} stable:{state.stable_frames} counted:{'YES' if state.counted else 'NO'}"
            (text_width, text_height), baseline = cv2.getTextSize(
                label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness
            )
            if text_width + 12 > right - left:
                label = f"P#{person_id}"
                (text_width, text_height), baseline = cv2.getTextSize(
                    label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness
                )
            label_top = max(0, top - text_height - baseline - 8)
            label_right = min(right, left + text_width + 12)
            cv2.rectangle(image, (left, label_top), (label_right, top), color, -1)
            cv2.putText(
                image,
                label,
                (left + 6, max(text_height + 1, top - baseline - 4)),
                cv2.FONT_HERSHEY_SIMPLEX,
                font_scale,
                text_color,
                thickness,
                cv2.LINE_AA,
            )

        if flow_summary:
            current, today, increment = flow_summary
            summary = f"FLOW current:{current} today:{today}" + (f"  NEW VISITOR +{increment}" if increment else "")
            cv2.putText(image, summary, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (0, 229, 255), thickness, cv2.LINE_AA)
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])
        return encoded.tobytes() if ok else jpeg
    except Exception:
        logger.exception("Failed to draw person overlays")
        return jpeg


def draw_safety_overlays(
    jpeg: bytes,
    detections: list[SafetyOverlay],
    intrusion_zone: list[tuple[float, float]] | None = None,
    people: list[PersonOverlay] | None = None,
    intruding_ids: set[int] | None = None,
) -> bytes:
    if not detections and not intrusion_zone:
        return jpeg
    try:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return jpeg
        height, width = image.shape[:2]
        thickness = max(2, round(min(width, height) / 260))
        font_scale = max(0.45, min(width, height) / 900)

        if intrusion_zone:
            polygon = np.asarray(
                [[round(x * width), round(y * height)] for x, y in intrusion_zone], dtype=np.int32
            )
            layer = image.copy()
            cv2.fillPoly(layer, [polygon], (45, 45, 210))
            cv2.addWeighted(layer, 0.20, image, 0.80, 0, image)
            cv2.polylines(image, [polygon], True, (45, 45, 235), thickness, cv2.LINE_AA)

        for person in people or []:
            if person.track_id is None or person.track_id not in (intruding_ids or set()):
                continue
            x1, y1, x2, y2 = person.box
            cv2.rectangle(
                image,
                (round(x1 * width), round(y1 * height)),
                (round(x2 * width), round(y2 * height)),
                (40, 40, 245),
                thickness + 1,
                cv2.LINE_AA,
            )

        for detection in detections:
            x1, y1, x2, y2 = detection.box
            color = (35, 35, 245) if detection.kind == "fire" else (30, 150, 255)
            left, top = round(x1 * width), round(y1 * height)
            right, bottom = round(x2 * width), round(y2 * height)
            cv2.rectangle(image, (left, top), (right, bottom), color, thickness + 1, cv2.LINE_AA)
            cv2.putText(
                image,
                f"{detection.kind.upper()} {detection.confidence:.0%}",
                (left + 4, max(18, top - 7)),
                cv2.FONT_HERSHEY_SIMPLEX,
                font_scale,
                color,
                thickness,
                cv2.LINE_AA,
            )
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])
        return encoded.tobytes() if ok else jpeg
    except Exception:
        logger.exception("Failed to draw safety overlays")
        return jpeg


class CameraStream:
    def __init__(
        self,
        camera_id: str,
        source: str,
        status_callback: Callable[[str, bool, str | None], None],
        output_fps: float = 2.0,
    ):
        self.camera_id = camera_id
        self.source = source
        self.status_callback = status_callback
        self.output_fps = max(1.0, min(output_fps, 10.0))
        self.frames: deque[FramePacket] = deque(maxlen=40)
        self.task: asyncio.Task | None = None
        self.running = False
        self.sequence = 0
        self.last_error = ""
        self.person_overlays: list[PersonOverlay] = []
        self.overlay_updated_at = 0.0
        self.safety_overlays: list[SafetyOverlay] = []
        self.safety_updated_at = 0.0
        self.object_overlays: list[ObjectOverlay] = []
        self.object_updated_at = 0.0
        self.intrusion_zone: list[tuple[float, float]] = []
        self.intruding_ids: set[int] = set()

    def start(self) -> None:
        if self.task and not self.task.done():
            return
        self.running = True
        self.task = asyncio.create_task(self._run(), name=f"media-{self.camera_id}")

    async def stop(self) -> None:
        self.running = False
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def latest(self) -> FramePacket | None:
        return self.frames[-1] if self.frames else None

    def sample(self, count: int = 8) -> list[FramePacket]:
        if not self.frames:
            return []
        if len(self.frames) <= count:
            return list(self.frames)
        step = (len(self.frames) - 1) / max(1, count - 1)
        return [self.frames[round(index * step)] for index in range(count)]

    def set_person_detections(self, detections: list[Detection]) -> None:
        self.person_overlays = [
            PersonOverlay(item.box, item.confidence, item.track_id) for item in detections
        ]
        self.overlay_updated_at = time.monotonic()

    def preview_jpeg(self, frame: FramePacket) -> bytes:
        people = self.person_overlays if time.monotonic() - self.overlay_updated_at <= 3.0 else []
        safety = self.safety_overlays if time.monotonic() - self.safety_updated_at <= 3.0 else []
        objects = self.object_overlays if time.monotonic() - self.object_updated_at <= 3.0 else []
        preview = draw_person_overlays(frame.jpeg, people)
        preview = draw_object_overlays(preview, objects)
        return draw_safety_overlays(
            preview, safety, self.intrusion_zone, people, self.intruding_ids
        )

    def set_safety_detections(self, detections: list[Detection]) -> None:
        self.safety_overlays = [SafetyOverlay(item.class_name, item.box, item.confidence) for item in detections]
        self.safety_updated_at = time.monotonic()

    def set_object_detections(self, detections: list[Detection]) -> None:
        self.object_overlays = phone_overlays(detections)
        self.object_updated_at = time.monotonic()

    def set_intrusion(self, zone: list[tuple[float, float]], track_ids: set[int]) -> None:
        self.intrusion_zone = zone
        self.intruding_ids = track_ids

    async def _run(self) -> None:
        delay = 1.0
        while self.running:
            try:
                await self._read_process()
                if self.running:
                    raise RuntimeError("视频流已结束")
            except asyncio.CancelledError:
                return
            except Exception as exc:
                safe = str(exc).replace(self.source, redact_rtsp(self.source))[:600]
                self.last_error = safe
                self.status_callback(self.camera_id, False, safe)
                logger.warning("Camera %s disconnected: %s", self.camera_id, safe)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
            else:
                delay = 1.0

    async def _read_process(self) -> None:
        source = self.source.removeprefix("file://") if self.source.startswith("file://") else self.source
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if self.source.startswith(("rtsp://", "rtsps://")):
            command += [
                "-rtsp_transport", "tcp", "-timeout", "10000000",
                "-fflags", "nobuffer", "-flags", "low_delay",
            ]
        if self.source.startswith("file://"):
            command += ["-stream_loop", "-1", "-re"]
        command += [
            "-i", source, "-an", "-vf", f"fps={self.output_fps:.3f},scale=-2:min(960\\,ih)",
            "-c:v", "mjpeg", "-q:v", "4", "-f", "image2pipe", "pipe:1",
        ]
        logger.info("Starting shared stream %s from %s", self.camera_id, redact_rtsp(self.source))
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        if process.stdout is None or process.stderr is None:
            process.kill()
            await process.wait()
            raise RuntimeError("FFmpeg 管道创建失败")
        buffer = bytearray()
        first_frame = True
        try:
            while self.running:
                chunk = await asyncio.wait_for(process.stdout.read(64 * 1024), timeout=15)
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    start = buffer.find(b"\xff\xd8")
                    end = buffer.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                    if start < 0 or end < 0:
                        if len(buffer) > 8 * 1024 * 1024:
                            buffer.clear()
                        break
                    jpeg = bytes(buffer[start:end + 2])
                    del buffer[:end + 2]
                    self.sequence += 1
                    self.frames.append(FramePacket(datetime.now(timezone.utc), jpeg, self.sequence))
                    if first_frame:
                        first_frame = False
                        self.last_error = ""
                        self.status_callback(self.camera_id, True, None)
        finally:
            if process.returncode is None:
                process.kill()
            try:
                _, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
            except asyncio.TimeoutError:
                stderr = b"FFmpeg stop timeout"
            if self.running:
                detail = stderr.decode("utf-8", errors="replace")[-500:]
                detail = detail.replace(self.source, redact_rtsp(self.source)).replace(source, redact_rtsp(self.source))
                raise RuntimeError(detail or "FFmpeg 无输出")


class MediaGateway:
    def __init__(self, status_callback: Callable[[str, bool, str | None], None]):
        self.status_callback = status_callback
        self.streams: dict[str, CameraStream] = {}

    async def sync(self, cameras: list[tuple[str, str, bool]]) -> None:
        wanted = {camera_id for camera_id, _, enabled in cameras if enabled}
        for camera_id in list(self.streams):
            source = next((item[1] for item in cameras if item[0] == camera_id), None)
            if camera_id not in wanted or source != self.streams[camera_id].source:
                await self.remove(camera_id)
        for camera_id, source, enabled in cameras:
            if enabled and camera_id not in self.streams:
                stream = CameraStream(camera_id, source, self.status_callback)
                self.streams[camera_id] = stream
                stream.start()

    async def remove(self, camera_id: str) -> None:
        stream = self.streams.pop(camera_id, None)
        if stream:
            await stream.stop()

    async def close(self) -> None:
        await asyncio.gather(*(stream.stop() for stream in list(self.streams.values())), return_exceptions=True)
        self.streams.clear()

    def latest(self, camera_id: str) -> FramePacket | None:
        stream = self.streams.get(camera_id)
        return stream.latest() if stream else None

    def sample(self, camera_id: str, count: int = 8) -> list[FramePacket]:
        stream = self.streams.get(camera_id)
        return stream.sample(count) if stream else []

    def set_person_detections(self, camera_id: str, detections: list[Detection]) -> None:
        stream = self.streams.get(camera_id)
        if stream:
            stream.set_person_detections(detections)

    def set_safety_detections(self, camera_id: str, detections: list[Detection]) -> None:
        stream = self.streams.get(camera_id)
        if stream:
            stream.set_safety_detections(detections)

    def set_object_detections(self, camera_id: str, detections: list[Detection]) -> None:
        stream = self.streams.get(camera_id)
        if stream:
            stream.set_object_detections(detections)

    def set_intrusion(self, camera_id: str, zone: list[tuple[float, float]], track_ids: set[int]) -> None:
        stream = self.streams.get(camera_id)
        if stream:
            stream.set_intrusion(zone, track_ids)

    def preview_jpeg(self, camera_id: str) -> bytes | None:
        stream = self.streams.get(camera_id)
        frame = stream.latest() if stream else None
        return stream.preview_jpeg(frame) if stream and frame else None

    async def mjpeg(self, camera_id: str, fps: float = 5.0) -> AsyncIterator[bytes]:
        last_sequence = -1
        while camera_id in self.streams:
            frame = self.latest(camera_id)
            if frame and frame.sequence != last_sequence:
                last_sequence = frame.sequence
                preview = self.preview_jpeg(camera_id) or frame.jpeg
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(preview)}\r\n\r\n".encode("ascii")
                    + preview + b"\r\n"
                )
            await asyncio.sleep(1 / max(1, fps))
