from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from backend.api.context import context
from backend.api.presenters import alert_public, analysis_public


router = APIRouter()


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": "1.0.0", **await context.require_runtime().status()}


@router.get("/api/dashboard")
async def dashboard() -> dict[str, Any]:
    return {
        **context.repository.dashboard(),
        "runtime": await context.require_runtime().status(),
    }


@router.get("/api/alerts")
async def alerts(
    limit: int = Query(default=100, ge=1, le=500),
    camera_id: str | None = None,
    mode: str | None = None,
    severity: str | None = None,
) -> list[dict[str, Any]]:
    rows = context.repository.list_alerts(limit, camera_id, mode, severity)
    return [alert_public(row) for row in rows]


@router.delete("/api/alerts")
async def delete_alerts(
    before_days: int = Query(default=30, ge=1, le=365),
    severity: str | None = None,
) -> dict[str, Any]:
    runtime = context.require_runtime()
    result = await asyncio.to_thread(runtime.cleanup.run, before_days, severity)
    return {
        "deleted": result["deleted"],
        "evidence_removed": result["evidence_removed"],
        "cutoff": result["cutoff"],
    }


@router.get("/api/analyses")
async def analyses(
    limit: int = Query(default=100, ge=1, le=500), camera_id: str | None = None
) -> list[dict[str, Any]]:
    return [analysis_public(row) for row in context.repository.list_analyses(limit, camera_id)]


@router.get("/api/traffic")
async def traffic(
    camera_id: str | None = None, limit: int = Query(default=1440, ge=1, le=10000)
) -> list[dict[str, Any]]:
    return [
        {
            "camera_id": row.camera_id,
            "bucket_start": row.bucket_start,
            "current_count": row.current_count,
            "entered": row.entered,
            "exited": row.exited,
        }
        for row in context.repository.traffic(camera_id, limit)
    ]


@router.get("/api/runtime/workers")
async def workers() -> dict[str, Any]:
    return await context.require_runtime().status()


@router.websocket("/ws/events")
async def events_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    runtime = context.require_runtime()
    queue = runtime.event_bus.subscribe()
    try:
        while True:
            try:
                await websocket.send_json(await asyncio.wait_for(queue.get(), timeout=25))
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "heartbeat"})
    except WebSocketDisconnect:
        pass
    finally:
        runtime.event_bus.unsubscribe(queue)
