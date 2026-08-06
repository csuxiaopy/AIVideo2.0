from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from backend import models
from backend.capabilities import capabilities_public, scene_templates_public
from backend.config import get_settings
from backend.database import upgrade_schema
from backend.pipeline import MonitoringRuntime
from backend.repository import Repository, as_json, from_json
from backend.schemas import (
    CameraCreate, CameraPatch, DetectorSettingsUpdate, GeometrySpec, Mode, ModesUpdate, ModelSettingsUpdate,
    ScheduleSpec, WebhookSettingsUpdate,
)
from backend.security import SecretCipher, redact_rtsp
from backend.vlm import VisionModelClient
from backend.webhook import WebhookClient


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
settings = get_settings()
repository = Repository()
cipher = SecretCipher(settings.app_encryption_key)
runtime: MonitoringRuntime | None = None


def current_runtime() -> MonitoringRuntime:
    if runtime is None:
        raise HTTPException(status_code=503, detail="运行时尚未初始化")
    return runtime


def camera_public(camera: models.Camera) -> dict[str, Any]:
    source = cipher.decrypt(camera.rtsp_url_encrypted)
    return {
        "id": camera.id, "name": camera.name, "enabled": camera.enabled,
        "scene_type": camera.scene_type,
        "source": redact_rtsp(source), "online": camera.online,
        "last_seen_at": camera.last_seen_at, "last_error": camera.last_error,
        "modes": from_json(camera.modes_json, []),
        "geometry": from_json(camera.geometry_json, {}),
        "schedule": from_json(camera.schedule_json, {}),
        "options": from_json(camera.options_json, {}),
        "created_at": camera.created_at, "updated_at": camera.updated_at,
    }


def alert_public(alert: models.Alert) -> dict[str, Any]:
    return {
        "id": alert.id, "camera_id": alert.camera_id, "analysis_id": alert.analysis_id,
        "mode": alert.mode, "status": alert.status, "confidence": alert.confidence,
        "severity": alert.severity, "zone_name": alert.zone_name,
        "local_model": alert.local_model, "model_version": alert.model_version,
        "reason": alert.reason, "evidence_path": alert.evidence_path,
        "evidence_url": f"/evidence/{alert.evidence_path}" if alert.evidence_path else None,
        "webhook_status": alert.webhook_status, "shadow": alert.shadow,
        "created_at": alert.created_at,
    }


def analysis_public(row: models.Analysis) -> dict[str, Any]:
    return {
        "id": row.id, "camera_id": row.camera_id, "mode": row.mode, "status": row.status,
        "confidence": row.confidence, "reason": row.reason, "evidence_path": row.evidence_path,
        "request_id": row.request_id, "provider": row.provider, "model": row.model,
        "severity": row.severity, "zone_name": row.zone_name,
        "local_model": row.local_model, "model_version": row.model_version,
        "usage": from_json(row.usage_json, {}), "error": row.error,
        "latency_ms": row.latency_ms, "created_at": row.created_at,
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    global runtime
    upgrade_schema()
    runtime = MonitoringRuntime(settings, repository, cipher)
    await runtime.start()
    yield
    await runtime.close()
    runtime = None


app = FastAPI(title="YOLO + 视觉大模型监控平台", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, Any]:
    state = await current_runtime().status()
    return {"status": "ok", "version": "1.0.0", **state}


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/cameras")
async def list_cameras() -> list[dict[str, Any]]:
    return [camera_public(camera) for camera in repository.list_cameras()]


@app.get("/api/scene-templates")
async def scene_templates() -> list[dict[str, Any]]:
    return scene_templates_public()


@app.get("/api/capabilities")
async def capabilities() -> list[dict[str, Any]]:
    return capabilities_public()


@app.post("/api/cameras", status_code=status.HTTP_201_CREATED)
async def create_camera(payload: CameraCreate) -> dict[str, Any]:
    if repository.get_camera(payload.id):
        raise HTTPException(status_code=409, detail="摄像头 ID 已存在")
    camera = models.Camera(
        id=payload.id, name=payload.name, rtsp_url_encrypted=cipher.encrypt(payload.rtsp_url),
        scene_type=payload.scene_type.value,
        enabled=payload.enabled, modes_json=as_json([mode.value for mode in payload.modes]),
        geometry_json=payload.geometry.model_dump_json(), schedule_json=payload.schedule.model_dump_json(),
        options_json=payload.options.model_dump_json(),
    )
    repository.create_camera(camera)
    await current_runtime().sync_cameras()
    return camera_public(camera)


@app.get("/api/cameras/{camera_id}")
async def get_camera(camera_id: str) -> dict[str, Any]:
    camera = repository.get_camera(camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="摄像头不存在")
    return camera_public(camera)


@app.patch("/api/cameras/{camera_id}")
async def patch_camera(camera_id: str, payload: CameraPatch) -> dict[str, Any]:
    camera = repository.get_camera(camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="摄像头不存在")
    values = payload.model_dump(exclude_none=True, exclude={"rtsp_url", "options"})
    if payload.rtsp_url:
        values["rtsp_url_encrypted"] = cipher.encrypt(payload.rtsp_url)
    if payload.options:
        values["options_json"] = payload.options.model_dump_json()
    updated = repository.update_camera(camera_id, values)
    await current_runtime().sync_cameras()
    return camera_public(updated)


@app.delete("/api/cameras/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera(camera_id: str) -> Response:
    if not repository.delete_camera(camera_id):
        raise HTTPException(status_code=404, detail="摄像头不存在")
    await current_runtime().media.remove(camera_id)
    current_runtime().rules.remove(camera_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def validate_camera_combination(camera: models.Camera, modes: list[Mode], geometry: GeometrySpec) -> None:
    CameraCreate(
        id=camera.id, name=camera.name, rtsp_url=cipher.decrypt(camera.rtsp_url_encrypted),
        enabled=camera.enabled, scene_type=camera.scene_type, modes=modes, geometry=geometry,
        schedule=ScheduleSpec.model_validate(from_json(camera.schedule_json, {})),
        options=from_json(camera.options_json, {}),
    )


@app.put("/api/cameras/{camera_id}/modes")
async def update_modes(camera_id: str, payload: ModesUpdate) -> dict[str, Any]:
    camera = repository.get_camera(camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="摄像头不存在")
    geometry = GeometrySpec.model_validate(from_json(camera.geometry_json, {}))
    validate_camera_combination(camera, payload.modes, geometry)
    updated = repository.update_camera(camera_id, {"modes_json": as_json([mode.value for mode in payload.modes])})
    return camera_public(updated)


@app.put("/api/cameras/{camera_id}/geometry")
async def update_geometry(camera_id: str, payload: GeometrySpec) -> dict[str, Any]:
    camera = repository.get_camera(camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="摄像头不存在")
    modes = [Mode(value) for value in from_json(camera.modes_json, [])]
    validate_camera_combination(camera, modes, payload)
    updated = repository.update_camera(camera_id, {"geometry_json": payload.model_dump_json()})
    return camera_public(updated)


@app.put("/api/cameras/{camera_id}/schedule")
async def update_schedule(camera_id: str, payload: ScheduleSpec) -> dict[str, Any]:
    if not repository.get_camera(camera_id):
        raise HTTPException(status_code=404, detail="摄像头不存在")
    updated = repository.update_camera(camera_id, {"schedule_json": payload.model_dump_json()})
    return camera_public(updated)


@app.post("/api/cameras/{camera_id}/analyze")
async def analyze_camera(camera_id: str) -> dict[str, Any]:
    try:
        return await current_runtime().analyze_now(camera_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="摄像头不存在")
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/cameras/{camera_id}/preview")
async def preview(camera_id: str) -> StreamingResponse:
    if not repository.get_camera(camera_id):
        raise HTTPException(status_code=404, detail="摄像头不存在")
    if camera_id not in current_runtime().media.streams:
        raise HTTPException(status_code=503, detail="视频流尚未连接")
    return StreamingResponse(
        current_runtime().media.mjpeg(camera_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/api/cameras/{camera_id}/snapshot")
async def snapshot(camera_id: str) -> Response:
    if not repository.get_camera(camera_id):
        raise HTTPException(status_code=404, detail="摄像头不存在")
    jpeg = current_runtime().media.preview_jpeg(camera_id)
    if not jpeg:
        raise HTTPException(status_code=503, detail="尚无可用画面")
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/dashboard")
async def dashboard() -> dict[str, Any]:
    return {**repository.dashboard(), "runtime": await current_runtime().status()}


@app.get("/api/alerts")
async def alerts(
    limit: int = Query(default=100, ge=1, le=500),
    camera_id: str | None = None,
    mode: str | None = None,
    severity: str | None = None,
) -> list[dict[str, Any]]:
    return [alert_public(row) for row in repository.list_alerts(limit, camera_id, mode, severity)]


@app.get("/api/analyses")
async def analyses(
    limit: int = Query(default=100, ge=1, le=500), camera_id: str | None = None
) -> list[dict[str, Any]]:
    return [analysis_public(row) for row in repository.list_analyses(limit, camera_id)]


@app.get("/api/traffic")
async def traffic(camera_id: str | None = None, limit: int = Query(default=1440, ge=1, le=10000)):
    return [
        {
            "camera_id": row.camera_id, "bucket_start": row.bucket_start,
            "current_count": row.current_count, "entered": row.entered, "exited": row.exited,
        }
        for row in repository.traffic(camera_id, limit)
    ]


@app.get("/api/runtime/workers")
async def workers() -> dict[str, Any]:
    return await current_runtime().status()


@app.get("/api/settings/models")
async def get_models() -> dict[str, Any]:
    row = repository.get_model_settings()
    return {
        "provider": row.provider, "base_url": row.base_url,
        "economy_model": row.economy_model, "enhanced_model": row.enhanced_model,
        "api_key_configured": bool(row.api_key_encrypted), "updated_at": row.updated_at,
    }


@app.get("/api/settings/detectors")
async def get_detectors() -> dict[str, Any]:
    row = repository.get_detector_settings()
    runtime_status = await current_runtime().status()
    return {
        "general_model": row.general_model,
        "general_device": row.general_device,
        "fire_smoke_model": row.fire_smoke_model,
        "fire_smoke_device": row.fire_smoke_device,
        "model_sha256": row.model_sha256,
        "license_name": row.license_name,
        "updated_at": row.updated_at,
        "runtime": runtime_status["detectors"],
    }


@app.put("/api/settings/detectors")
async def put_detectors(payload: DetectorSettingsUpdate) -> dict[str, Any]:
    repository.save_detector_settings(payload.model_dump())
    await current_runtime().reload_detectors()
    return await get_detectors()


@app.put("/api/settings/models")
async def put_models(payload: ModelSettingsUpdate) -> dict[str, Any]:
    existing = repository.get_model_settings()
    effective_key = payload.api_key or (
        cipher.decrypt(existing.api_key_encrypted) if existing.api_key_encrypted else ""
    )
    if payload.provider != "mock" and (not payload.base_url or not effective_key):
        raise HTTPException(status_code=400, detail="外部模型必须配置 Base URL 和 API Key")
    values = {
        "provider": payload.provider, "base_url": payload.base_url,
        "economy_model": payload.economy_model, "enhanced_model": payload.enhanced_model,
    }
    if payload.api_key:
        values["api_key_encrypted"] = cipher.encrypt(payload.api_key)
    repository.save_model_settings(values)
    await current_runtime().reload_models()
    return await get_models()


@app.post("/api/settings/models/test")
async def test_models() -> dict[str, Any]:
    row = repository.get_model_settings()
    if row.provider == "mock":
        return {"ok": True, "provider": "mock", "latency_ms": 0}
    if not current_runtime().vlm:
        raise HTTPException(status_code=400, detail="视觉大模型尚未完整配置")
    try:
        return await current_runtime().vlm.test()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/settings/webhook")
async def get_webhook() -> dict[str, Any]:
    row = repository.get_webhook_settings()
    return {
        "enabled": row.enabled, "url": row.url,
        "secret_configured": bool(row.secret_encrypted), "updated_at": row.updated_at,
    }


@app.put("/api/settings/webhook")
async def put_webhook(payload: WebhookSettingsUpdate) -> dict[str, Any]:
    existing = repository.get_webhook_settings()
    effective_secret = payload.secret or (
        cipher.decrypt(existing.secret_encrypted) if existing.secret_encrypted else ""
    )
    if payload.enabled and (not payload.url.startswith("https://") or not effective_secret):
        raise HTTPException(status_code=400, detail="启用 Webhook 时必须配置 HTTPS URL 和密钥")
    values: dict[str, Any] = {"enabled": payload.enabled, "url": payload.url}
    if payload.secret:
        values["secret_encrypted"] = cipher.encrypt(payload.secret)
    repository.save_webhook_settings(values)
    return await get_webhook()


@app.post("/api/settings/webhook/test")
async def test_webhook() -> dict[str, Any]:
    row = repository.get_webhook_settings()
    if not row.url or not row.secret_encrypted:
        raise HTTPException(status_code=400, detail="Webhook 尚未完整配置")
    client = WebhookClient()
    try:
        await client.send(row.url, cipher.decrypt(row.secret_encrypted), {"type": "test", "message": "YOLO VLM monitor webhook test"}, attempts=1)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.close()


@app.websocket("/ws/events")
async def events_socket(websocket: WebSocket):
    await websocket.accept()
    queue = current_runtime().event_bus.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=25)
                await websocket.send_json(event)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "heartbeat"})
    except WebSocketDisconnect:
        pass
    finally:
        current_runtime().event_bus.unsubscribe(queue)


@app.get("/evidence/{filename}")
async def evidence(filename: str):
    safe_name = filename.replace("\\", "/").split("/")[-1]
    path = settings.evidence_dir / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="证据图片不存在")
    return FileResponse(path)


if settings.web_dist_dir.exists():
    assets = settings.web_dist_dir / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", response_class=HTMLResponse)
    async def spa(path: str):
        candidate = settings.web_dist_dir / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(settings.web_dist_dir / "index.html")
else:
    @app.get("/", response_class=HTMLResponse)
    async def placeholder() -> str:
        return "<h1>YOLO + 视觉大模型监控平台</h1><p>前端尚未构建，请运行 frontend 的 npm run build。</p>"
