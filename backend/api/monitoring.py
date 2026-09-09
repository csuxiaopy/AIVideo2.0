from __future__ import annotations

import asyncio
from datetime import date as date_type
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.drawing.image import Image as ExcelImage
from openpyxl.styles import Alignment, Font

from backend.api.context import context
from backend.api.presenters import alert_public, analysis_public
from backend.schemas import AlertBatchDelete, AlertExportRequest, WebhookManualSend
from backend.capabilities import CORE_CAPABILITIES
from backend.auth import admin_user, current_user, websocket_user


router = APIRouter()


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": "1.0.0", **await context.require_runtime().status()}


@router.get("/api/dashboard", dependencies=[Depends(current_user)])
async def dashboard() -> dict[str, Any]:
    return {
        **context.repository.dashboard(),
        "runtime": await context.require_runtime().status(),
    }


@router.get("/api/alerts", dependencies=[Depends(current_user)])
async def alerts(
    limit: int = Query(default=100, ge=1, le=500),
    camera_id: str | None = None,
    mode: str | None = None,
    severity: str | None = None,
    date: date_type | None = None,
) -> list[dict[str, Any]]:
    rows = context.repository.list_alerts(limit, camera_id, mode, severity, date)
    return [alert_public(row, context.repository.webhook_deliveries(row.id)) for row in rows]


def _alert_workbook(rows: list[Any]) -> BytesIO:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "告警记录"
    sheet.append(["摄像头名称", "事件", "原因", "证据", "时间"])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")
    sheet.column_dimensions["A"].width = 24
    sheet.column_dimensions["B"].width = 18
    sheet.column_dimensions["C"].width = 48
    sheet.column_dimensions["D"].width = 24
    sheet.column_dimensions["E"].width = 22
    for row_index, alert in enumerate(rows, start=2):
        created_at = context.repository._aware_utc(alert.created_at).astimezone(
            ZoneInfo("Asia/Shanghai")
        ).strftime("%Y-%m-%d %H:%M:%S")
        mode_name = CORE_CAPABILITIES.get(alert.mode, {}).get("name", alert.mode)
        sheet.append([getattr(alert, "camera_name", alert.camera_id), mode_name, alert.reason, "无证据", created_at])
        sheet.row_dimensions[row_index].height = 76
        if alert.evidence_path:
            root = context.settings.evidence_dir.resolve()
            evidence = (root / Path(alert.evidence_path).name).resolve()
            try:
                evidence.relative_to(root)
                if evidence.is_file():
                    image = ExcelImage(str(evidence))
                    image.width, image.height = 120, 90
                    sheet.add_image(image, f"D{row_index}")
                    sheet.cell(row_index, 4).value = ""
            except (ValueError, OSError):
                pass
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output


@router.post("/api/alerts/export", dependencies=[Depends(current_user)])
async def export_alerts(payload: AlertExportRequest) -> StreamingResponse:
    alert_date = None
    if payload.date:
        try:
            alert_date = date_type.fromisoformat(payload.date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="日期格式必须为 YYYY-MM-DD") from exc
    rows = context.repository.list_alerts(
        None, mode=payload.mode, severity=payload.severity,
        alert_date=alert_date, alert_ids=payload.alert_ids,
    )
    filename = f"alerts-{payload.date or 'all'}.xlsx"
    workbook = await asyncio.to_thread(_alert_workbook, rows)
    return StreamingResponse(
        workbook,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/alert-webhook-targets", dependencies=[Depends(current_user)])
async def alert_webhook_targets() -> dict[str, Any]:
    return {"items": [
        {"id": row.id, "name": row.name, "enabled": row.enabled}
        for row in context.repository.list_webhook_targets(enabled_only=True)
    ]}


@router.post("/api/alerts/webhook-send", dependencies=[Depends(current_user)])
async def send_alerts_to_webhooks(payload: WebhookManualSend) -> dict[str, int]:
    try:
        return await context.require_runtime().alerts.manual_send(payload.alert_ids, payload.webhook_target_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/alerts", dependencies=[Depends(admin_user)])
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


@router.post("/api/alerts/batch-delete", dependencies=[Depends(admin_user)])
async def delete_selected_alerts(payload: AlertBatchDelete) -> dict[str, Any]:
    runtime = context.require_runtime()
    return await asyncio.to_thread(runtime.cleanup.delete_selected, payload.alert_ids)


@router.get("/api/analyses", dependencies=[Depends(admin_user)])
async def analyses(
    limit: int = Query(default=100, ge=1, le=500), camera_id: str | None = None
) -> list[dict[str, Any]]:
    return [analysis_public(row) for row in context.repository.list_analyses(limit, camera_id)]


@router.get("/api/traffic", dependencies=[Depends(current_user)])
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


@router.get("/api/traffic/summary", dependencies=[Depends(current_user)])
async def traffic_summary() -> dict[str, Any]:
    return context.repository.traffic_summary()


@router.get("/api/runtime/workers", dependencies=[Depends(admin_user)])
async def workers() -> dict[str, Any]:
    return await context.require_runtime().status()


@router.websocket("/ws/events")
async def events_socket(websocket: WebSocket) -> None:
    if websocket_user(websocket) is None:
        await websocket.close(code=4401)
        return
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
