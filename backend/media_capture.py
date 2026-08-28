from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Callable

from backend.media import (
    FramePacket,
    PersonOverlay,
    SafetyOverlay,
    draw_person_overlays,
    draw_safety_overlays,
)
from backend.schemas import Detection
from backend.security import redact_rtsp


logger = logging.getLogger(__name__)


class PreviewLimitError(RuntimeError):
    pass


def _input_args(source: str) -> tuple[list[str], str]:
    resolved = source.removeprefix("file://") if source.startswith("file://") else source
    args: list[str] = []
    if source.startswith(("rtsp://", "rtsps://")):
        args += ["-rtsp_transport", "tcp", "-timeout", "10000000"]
    if source.startswith("file://"):
        args += ["-stream_loop", "-1", "-re"]
    return args, resolved


async def _terminate_process(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    process.kill()
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
    except asyncio.TimeoutError:
        logger.warning("FFmpeg process did not exit after kill")


class LivePreviewStream:
    def __init__(self, camera_id: str, source: str, output_fps: float, timeout_seconds: int):
        self.camera_id = camera_id
        self.source = source
        self.output_fps = max(1.0, min(output_fps, 2.0))
        self.timeout_seconds = timeout_seconds
        self.sessions: dict[str, float] = {}
        self.frame: FramePacket | None = None
        self.sequence = 0
        self.started_at = datetime.now(timezone.utc)
        self.task: asyncio.Task | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.running = False
        self.last_error = ""

    def add_session(self) -> str:
        session_id = uuid.uuid4().hex
        self.sessions[session_id] = time.monotonic()
        return session_id

    def touch(self, session_id: str) -> bool:
        if session_id not in self.sessions:
            return False
        self.sessions[session_id] = time.monotonic()
        return True

    def expire_sessions(self, now: float) -> list[str]:
        return [
            session_id
            for session_id, heartbeat_at in self.sessions.items()
            if now - heartbeat_at > self.timeout_seconds
        ]

    def start(self) -> None:
        if self.task and not self.task.done():
            return
        self.running = True
        self.task = asyncio.create_task(self._run(), name=f"live-preview-{self.camera_id}")

    async def stop(self) -> None:
        self.running = False
        self.sessions.clear()
        await _terminate_process(self.process)
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self.process = None
        self.frame = None

    async def _run(self) -> None:
        delay = 1.0
        while self.running and self.sessions:
            try:
                await self._read_process()
                if self.running and self.sessions:
                    raise RuntimeError("实时预览流已结束")
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.last_error = str(exc).replace(self.source, redact_rtsp(self.source))[-600:]
                logger.warning("Preview %s disconnected: %s", self.camera_id, self.last_error)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)
            else:
                delay = 1.0

    async def _read_process(self) -> None:
        input_args, resolved = _input_args(self.source)
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", *input_args]
        command += [
            "-i", resolved, "-an",
            "-vf", f"fps={self.output_fps:.3f},scale=-2:min(960\\,ih)",
            "-c:v", "mjpeg", "-q:v", "4", "-f", "image2pipe", "pipe:1",
        ]
        logger.info("Starting on-demand preview %s from %s", self.camera_id, redact_rtsp(self.source))
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        self.process = process
        if process.stdout is None or process.stderr is None:
            await _terminate_process(process)
            raise RuntimeError("FFmpeg 实时预览管道创建失败")
        buffer = bytearray()
        try:
            while self.running and self.sessions:
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
                    jpeg = bytes(buffer[start : end + 2])
                    del buffer[: end + 2]
                    self.sequence += 1
                    self.frame = FramePacket(datetime.now(timezone.utc), jpeg, self.sequence)
        finally:
            await _terminate_process(process)
            if self.process is process:
                self.process = None


class MediaGateway:
    """Periodic single-frame capture plus leased, on-demand live previews."""

    def __init__(
        self,
        status_callback: Callable[..., None],
        snapshot_dir: Path,
        max_live_previews: int = 4,
        preview_fps: float = 2.0,
        preview_timeout_seconds: int = 60,
        capture_timeout_seconds: int = 15,
    ):
        self.status_callback = status_callback
        self.snapshot_dir = snapshot_dir
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.max_live_previews = max(1, max_live_previews)
        self.preview_fps = preview_fps
        self.preview_timeout_seconds = max(10, preview_timeout_seconds)
        self.capture_timeout_seconds = max(5, capture_timeout_seconds)
        self.sources: dict[str, str] = {}
        self.snapshots: dict[str, deque[FramePacket]] = defaultdict(lambda: deque(maxlen=8))
        self.capture_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.previews: dict[str, LivePreviewStream] = {}
        self.session_camera: dict[str, str] = {}
        self.preview_lock = asyncio.Lock()
        self.sweeper_task: asyncio.Task | None = None
        self.person_overlays: dict[str, tuple[float, list[PersonOverlay]]] = {}
        self.safety_overlays: dict[str, tuple[float, list[SafetyOverlay]]] = {}
        self.intrusions: dict[str, tuple[list[tuple[float, float]], set[int]]] = {}

    @property
    def streams(self) -> dict[str, LivePreviewStream]:
        """Compatibility alias. Only active on-demand previews are streams now."""
        return self.previews

    async def start(self) -> None:
        if not self.sweeper_task or self.sweeper_task.done():
            self.sweeper_task = asyncio.create_task(self._preview_sweeper(), name="preview-sweeper")

    async def sync(self, cameras: list[tuple[str, str, bool]]) -> None:
        wanted = {camera_id: source for camera_id, source, enabled in cameras if enabled}
        to_stop = [
            camera_id
            for camera_id, preview in self.previews.items()
            if camera_id not in wanted or preview.source != wanted[camera_id]
        ]
        for camera_id in to_stop:
            await self.stop_camera_preview(camera_id)
        self.sources = wanted

    async def remove(self, camera_id: str) -> None:
        self.sources.pop(camera_id, None)
        await self.stop_camera_preview(camera_id)
        self.snapshots.pop(camera_id, None)
        self.capture_locks.pop(camera_id, None)
        self.person_overlays.pop(camera_id, None)
        self.safety_overlays.pop(camera_id, None)
        self.intrusions.pop(camera_id, None)
        path = self._snapshot_path(camera_id)
        if path.exists():
            await asyncio.to_thread(path.unlink)

    async def close(self) -> None:
        if self.sweeper_task:
            self.sweeper_task.cancel()
            await asyncio.gather(self.sweeper_task, return_exceptions=True)
            self.sweeper_task = None
        streams = list(self.previews.values())
        self.previews.clear()
        self.session_camera.clear()
        await asyncio.gather(*(stream.stop() for stream in streams), return_exceptions=True)
        self.snapshots.clear()

    async def capture(self, camera_id: str) -> FramePacket:
        source = self.sources.get(camera_id)
        if not source:
            raise RuntimeError("摄像头未启用或视频源不存在")
        async with self.capture_locks[camera_id]:
            try:
                jpeg = await self._grab_single_frame(source)
                packet = FramePacket(datetime.now(timezone.utc), jpeg, self._next_sequence(camera_id))
                self.snapshots[camera_id].append(packet)
                await asyncio.to_thread(self._save_snapshot, camera_id, jpeg)
                self.status_callback(camera_id, True, None, packet.captured_at)
                return packet
            except Exception as exc:
                safe = str(exc).replace(source, redact_rtsp(source))[:1000]
                self.status_callback(camera_id, False, safe, None)
                raise RuntimeError(safe) from exc

    async def _grab_single_frame(self, source: str) -> bytes:
        input_args, resolved = _input_args(source)
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", *input_args]
        command += [
            "-i", resolved, "-an", "-frames:v", "1",
            "-vf", "scale=-2:min(960\\,ih)",
            "-c:v", "mjpeg", "-q:v", "4", "-f", "image2pipe", "pipe:1",
        ]
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.capture_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            await _terminate_process(process)
            raise RuntimeError("抓取单帧超时") from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[-600:]
            raise RuntimeError(detail or f"FFmpeg 抓帧失败，退出码 {process.returncode}")
        start = stdout.find(b"\xff\xd8")
        end = stdout.rfind(b"\xff\xd9")
        if start < 0 or end < start:
            raise RuntimeError("FFmpeg 未返回有效 JPEG 画面")
        return stdout[start : end + 2]

    def latest(self, camera_id: str) -> FramePacket | None:
        frames = self.snapshots.get(camera_id)
        if frames:
            return frames[-1]
        path = self._snapshot_path(camera_id)
        if not path.is_file():
            return None
        try:
            captured_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            packet = FramePacket(captured_at, path.read_bytes(), 1)
            self.snapshots[camera_id].append(packet)
            return packet
        except OSError:
            logger.exception("Failed to load snapshot for %s", camera_id)
            return None

    def sample(self, camera_id: str, count: int = 8) -> list[FramePacket]:
        return list(self.snapshots.get(camera_id, []))[-count:]

    def preview_jpeg(self, camera_id: str) -> bytes | None:
        frame = self.latest(camera_id)
        return self._decorate(camera_id, frame.jpeg) if frame else None

    def set_person_detections(self, camera_id: str, detections: list[Detection]) -> None:
        self.person_overlays[camera_id] = (
            time.monotonic(),
            [PersonOverlay(item.box, item.confidence, item.track_id) for item in detections],
        )

    def set_safety_detections(self, camera_id: str, detections: list[Detection]) -> None:
        self.safety_overlays[camera_id] = (
            time.monotonic(),
            [SafetyOverlay(item.class_name, item.box, item.confidence) for item in detections],
        )

    def set_intrusion(
        self, camera_id: str, zone: list[tuple[float, float]], track_ids: set[int]
    ) -> None:
        self.intrusions[camera_id] = (zone, track_ids)

    async def start_preview(self, camera_id: str) -> dict[str, object]:
        source = self.sources.get(camera_id)
        if not source:
            raise RuntimeError("摄像头未启用或视频源不存在")
        async with self.preview_lock:
            stream = self.previews.get(camera_id)
            if stream is None:
                if len(self.previews) >= self.max_live_previews:
                    raise PreviewLimitError(
                        "当前实时预览数量已达到上限，请关闭其他预览后重试。"
                    )
                stream = LivePreviewStream(
                    camera_id, source, self.preview_fps, self.preview_timeout_seconds
                )
                self.previews[camera_id] = stream
                session_id = stream.add_session()
                stream.start()
            else:
                session_id = stream.add_session()
            self.session_camera[session_id] = camera_id
        return {
            "camera_id": camera_id,
            "session_id": session_id,
            "stream_url": f"/api/cameras/{camera_id}/preview?session_id={session_id}",
            "heartbeat_interval_seconds": min(20, self.preview_timeout_seconds // 3),
            "timeout_seconds": self.preview_timeout_seconds,
        }

    async def heartbeat_preview(self, session_id: str) -> bool:
        async with self.preview_lock:
            camera_id = self.session_camera.get(session_id)
            stream = self.previews.get(camera_id) if camera_id else None
            return bool(stream and stream.touch(session_id))

    async def stop_preview(self, session_id: str) -> bool:
        stream_to_stop: LivePreviewStream | None = None
        async with self.preview_lock:
            camera_id = self.session_camera.pop(session_id, None)
            stream = self.previews.get(camera_id) if camera_id else None
            if not stream:
                return False
            stream.sessions.pop(session_id, None)
            if not stream.sessions:
                self.previews.pop(camera_id, None)
                stream_to_stop = stream
        if stream_to_stop:
            await stream_to_stop.stop()
        return True

    async def stop_camera_preview(self, camera_id: str) -> None:
        async with self.preview_lock:
            stream = self.previews.pop(camera_id, None)
            if stream:
                for session_id in list(stream.sessions):
                    self.session_camera.pop(session_id, None)
        if stream:
            await stream.stop()

    async def mjpeg(self, camera_id: str, session_id: str) -> AsyncIterator[bytes]:
        last_sequence = -1
        try:
            while True:
                async with self.preview_lock:
                    stream = self.previews.get(camera_id)
                    valid = bool(
                        stream
                        and self.session_camera.get(session_id) == camera_id
                        and stream.touch(session_id)
                    )
                    frame = stream.frame if stream else None
                if not valid:
                    return
                if frame and frame.sequence != last_sequence:
                    last_sequence = frame.sequence
                    jpeg = self._decorate(camera_id, frame.jpeg)
                    yield (
                        b"--frame\r\nContent-Type: image/jpeg\r\n"
                        + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
                        + jpeg + b"\r\n"
                    )
                await asyncio.sleep(0.2)
        finally:
            await self.stop_preview(session_id)

    def preview_status(self, camera_id: str) -> dict[str, object]:
        stream = self.previews.get(camera_id)
        return {
            "active": bool(stream),
            "started_at": stream.started_at if stream else None,
            "clients": len(stream.sessions) if stream else 0,
        }

    async def _preview_sweeper(self) -> None:
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            expired: list[str] = []
            async with self.preview_lock:
                for stream in self.previews.values():
                    expired.extend(stream.expire_sessions(now))
            for session_id in expired:
                logger.info("Releasing expired preview session %s", session_id[:8])
                await self.stop_preview(session_id)

    def _decorate(self, camera_id: str, jpeg: bytes) -> bytes:
        now = time.monotonic()
        people_at, people = self.person_overlays.get(camera_id, (0.0, []))
        safety_at, safety = self.safety_overlays.get(camera_id, (0.0, []))
        zone, intruding_ids = self.intrusions.get(camera_id, ([], set()))
        people = people if now - people_at <= 3.0 else []
        safety = safety if now - safety_at <= 3.0 else []
        rendered = draw_person_overlays(jpeg, people)
        return draw_safety_overlays(rendered, safety, zone, people, intruding_ids)

    def _snapshot_path(self, camera_id: str) -> Path:
        return self.snapshot_dir / f"{camera_id}.jpg"

    def _save_snapshot(self, camera_id: str, jpeg: bytes) -> None:
        path = self._snapshot_path(camera_id)
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(jpeg)
        os.replace(temporary, path)

    def _next_sequence(self, camera_id: str) -> int:
        frames = self.snapshots.get(camera_id)
        return (frames[-1].sequence + 1) if frames else 1
