from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse

from backend import models
from backend.api.context import context
from backend.api.presenters import camera_public
from backend.capabilities import capabilities_public, scene_templates_public
from backend.media_capture import PreviewLimitError
from backend.repository import as_json, from_json
from backend.schemas import (
    CameraCreate,
    CameraPatch,
    GeometrySpec,
    Mode,
    ModesUpdate,
    PreviewSessionRequest,
    ScheduleSpec,
)


router = APIRouter(prefix="/api")


def _camera_or_404(camera_id: str) -> models.Camera:
    camera = context.repository.get_camera(camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="摄像头不存在")
    return camera


def _public(camera: models.Camera | None) -> dict[str, Any]:
    if camera is None:
        raise HTTPException(status_code=404, detail="摄像头不存在")
    preview_status = context.runtime.media.preview_status(camera.id) if context.runtime else None
    return camera_public(camera, context.cipher, preview_status)


def _validate_combination(camera: models.Camera, modes: list[Mode], geometry: GeometrySpec) -> None:
    CameraCreate(
        id=camera.id,
        name=camera.name,
        rtsp_url=context.cipher.decrypt(camera.rtsp_url_encrypted),
        enabled=camera.enabled,
        scene_type=camera.scene_type,
        modes=modes,
        geometry=geometry,
        schedule=ScheduleSpec.model_validate(from_json(camera.schedule_json, {})),
        options=from_json(camera.options_json, {}),
        frame_interval_seconds=camera.frame_interval_seconds,
    )


def _effective_patch(camera: models.Camera, payload: CameraPatch) -> CameraCreate:
    return CameraCreate(
        id=payload.id or camera.id,
        name=payload.name or camera.name,
        rtsp_url=payload.rtsp_url or context.cipher.decrypt(camera.rtsp_url_encrypted),
        enabled=payload.enabled if payload.enabled is not None else camera.enabled,
        scene_type=payload.scene_type or camera.scene_type,
        modes=payload.modes if payload.modes is not None else from_json(camera.modes_json, []),
        geometry=payload.geometry or from_json(camera.geometry_json, {}),
        schedule=payload.schedule or from_json(camera.schedule_json, {}),
        options=payload.options or from_json(camera.options_json, {}),
        frame_interval_seconds=payload.frame_interval_seconds or camera.frame_interval_seconds,
    )


@router.get("/cameras")
async def list_cameras() -> list[dict[str, Any]]:
    return [_public(camera) for camera in context.repository.list_cameras()]


@router.get("/scene-templates")
async def scene_templates() -> list[dict[str, Any]]:
    return scene_templates_public()


@router.get("/capabilities")
async def capabilities() -> list[dict[str, Any]]:
    return capabilities_public()


@router.post("/cameras", status_code=status.HTTP_201_CREATED)
async def create_camera(payload: CameraCreate) -> dict[str, Any]:
    if context.repository.get_camera(payload.id):
        raise HTTPException(status_code=409, detail="摄像头 ID 已存在")
    camera = models.Camera(
        id=payload.id,
        name=payload.name,
        rtsp_url_encrypted=context.cipher.encrypt(payload.rtsp_url),
        scene_type=payload.scene_type.value,
        enabled=payload.enabled,
        modes_json=as_json([mode.value for mode in payload.modes]),
        geometry_json=payload.geometry.model_dump_json(),
        schedule_json=payload.schedule.model_dump_json(),
        options_json=payload.options.model_dump_json(),
        frame_interval_seconds=payload.frame_interval_seconds,
    )
    context.repository.create_camera(camera)
    await context.require_runtime().sync_cameras()
    return _public(camera)


@router.get("/cameras/{camera_id}")
async def get_camera(camera_id: str) -> dict[str, Any]:
    return _public(_camera_or_404(camera_id))


@router.patch("/cameras/{camera_id}")
async def patch_camera(camera_id: str, payload: CameraPatch) -> dict[str, Any]:
    camera = _camera_or_404(camera_id)
    effective = _effective_patch(camera, payload)
    new_camera_id = effective.id
    if new_camera_id != camera_id and context.repository.get_camera(new_camera_id):
        raise HTTPException(status_code=409, detail="摄像头 ID 已存在")
    values = payload.model_dump(
        exclude_none=True,
        exclude={"id", "rtsp_url", "options", "modes", "geometry", "schedule"},
    )
    if "scene_type" in values:
        values["scene_type"] = effective.scene_type.value
    if payload.rtsp_url:
        values["rtsp_url_encrypted"] = context.cipher.encrypt(payload.rtsp_url)
    if payload.options:
        values["options_json"] = payload.options.model_dump_json()
    if payload.modes is not None:
        values["modes_json"] = as_json([mode.value for mode in payload.modes])
    if payload.geometry is not None:
        values["geometry_json"] = payload.geometry.model_dump_json()
    if payload.schedule is not None:
        values["schedule_json"] = payload.schedule.model_dump_json()
    if new_camera_id != camera_id:
        runtime = context.require_runtime()
        await runtime.media.remove(camera_id)
        runtime.rules.remove(camera_id)
        runtime.next_run.pop(camera_id, None)
        runtime.next_fire_run.pop(camera_id, None)
        runtime.queued.discard(camera_id)
        runtime.fire_queued.discard(camera_id)
        updated = context.repository.rename_and_update_camera(camera_id, new_camera_id, values)
    else:
        updated = context.repository.update_camera(camera_id, values)
    await context.require_runtime().sync_cameras()
    return _public(updated)


@router.delete("/cameras/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_camera(camera_id: str) -> Response:
    if not context.repository.delete_camera(camera_id):
        raise HTTPException(status_code=404, detail="摄像头不存在")
    runtime = context.require_runtime()
    await runtime.media.remove(camera_id)
    runtime.rules.remove(camera_id)
    runtime.next_run.pop(camera_id, None)
    runtime.next_fire_run.pop(camera_id, None)
    runtime.queued.discard(camera_id)
    runtime.fire_queued.discard(camera_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/cameras/{camera_id}/modes")
async def update_modes(camera_id: str, payload: ModesUpdate) -> dict[str, Any]:
    camera = _camera_or_404(camera_id)
    geometry = GeometrySpec.model_validate(from_json(camera.geometry_json, {}))
    _validate_combination(camera, payload.modes, geometry)
    updated = context.repository.update_camera(
        camera_id, {"modes_json": as_json([mode.value for mode in payload.modes])}
    )
    return _public(updated)


@router.put("/cameras/{camera_id}/geometry")
async def update_geometry(camera_id: str, payload: GeometrySpec) -> dict[str, Any]:
    camera = _camera_or_404(camera_id)
    modes = [Mode(value) for value in from_json(camera.modes_json, [])]
    _validate_combination(camera, modes, payload)
    return _public(
        context.repository.update_camera(camera_id, {"geometry_json": payload.model_dump_json()})
    )


@router.put("/cameras/{camera_id}/schedule")
async def update_schedule(camera_id: str, payload: ScheduleSpec) -> dict[str, Any]:
    _camera_or_404(camera_id)
    return _public(
        context.repository.update_camera(camera_id, {"schedule_json": payload.model_dump_json()})
    )


@router.post("/cameras/{camera_id}/analyze")
async def analyze_camera(camera_id: str) -> dict[str, Any]:
    try:
        return await context.require_runtime().analyze_now(camera_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="摄像头不存在")
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/cameras/{camera_id}/preview/start")
async def start_preview(camera_id: str) -> dict[str, object]:
    _camera_or_404(camera_id)
    try:
        return await context.require_runtime().media.start_preview(camera_id)
    except PreviewLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/cameras/{camera_id}/preview/heartbeat")
async def heartbeat_preview(camera_id: str, payload: PreviewSessionRequest) -> dict[str, bool]:
    _camera_or_404(camera_id)
    media = context.require_runtime().media
    active = media.session_camera.get(payload.session_id) == camera_id
    active = active and await media.heartbeat_preview(payload.session_id)
    if not active:
        raise HTTPException(status_code=404, detail="实时预览会话已结束，请重新打开。")
    return {"active": True}


@router.post("/cameras/{camera_id}/preview/stop", status_code=status.HTTP_204_NO_CONTENT)
async def stop_preview(camera_id: str, payload: PreviewSessionRequest) -> Response:
    _camera_or_404(camera_id)
    media = context.require_runtime().media
    if media.session_camera.get(payload.session_id) == camera_id:
        await media.stop_preview(payload.session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/cameras/{camera_id}/preview")
async def preview(camera_id: str, session_id: str = Query(min_length=16, max_length=100)) -> StreamingResponse:
    _camera_or_404(camera_id)
    runtime = context.require_runtime()
    if runtime.media.session_camera.get(session_id) != camera_id:
        raise HTTPException(status_code=404, detail="实时预览会话不存在或已过期")
    if not await runtime.media.heartbeat_preview(session_id):
        raise HTTPException(status_code=404, detail="实时预览会话不存在或已过期")
    return StreamingResponse(
        runtime.media.mjpeg(camera_id, session_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/cameras/{camera_id}/snapshot")
async def snapshot(camera_id: str) -> Response:
    _camera_or_404(camera_id)
    jpeg = context.require_runtime().media.preview_jpeg(camera_id)
    if not jpeg:
        raise HTTPException(status_code=503, detail="尚无可用画面")
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
