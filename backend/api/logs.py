from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from backend.api.context import context
from backend.api.presenters import analysis_public
from backend.auth import AdminUser
from backend.repository import from_json


router = APIRouter(prefix="/api/logs")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def audit_public(row, detail: bool = False) -> dict[str, Any]:
    result = {
        "id": row.id, "actor_user_id": row.actor_user_id,
        "actor_username": row.actor_username, "actor_display_name": row.actor_display_name,
        "action": row.action, "target_type": row.target_type, "target_id": row.target_id,
        "method": row.method, "path": row.path, "outcome": row.outcome,
        "status_code": row.status_code, "ip_address": row.ip_address,
        "created_at": row.created_at,
    }
    if detail:
        result.update(summary=from_json(row.summary_json, {}), user_agent=row.user_agent)
    return result


def model_call_public(row, detail: bool = False) -> dict[str, Any]:
    result = {
        "id": row.id, "camera_id": row.camera_id, "camera_name": row.camera_name,
        "modes": from_json(row.modes_json, []), "stage": row.stage,
        "provider": row.provider, "model": row.model, "request_id": row.request_id,
        "http_status": row.http_status, "outcome": row.outcome,
        "latency_ms": row.latency_ms, "usage": from_json(row.usage_json, {}),
        "error": row.error, "created_at": row.created_at,
    }
    if detail:
        result.update(raw_response=row.raw_response,
                      parsed_response=from_json(row.parsed_response_json, {}))
    return result


def _page(items: list[Any], total: int, page: int, page_size: int, presenter) -> dict[str, Any]:
    return {"items": [presenter(row) for row in items], "total": total,
            "page": page, "page_size": page_size}


@router.get("/audit")
async def audit_logs(
    _: AdminUser, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    start: datetime | None = None, end: datetime | None = None,
    username: str | None = None, action: str | None = None, outcome: str | None = None,
) -> dict[str, Any]:
    rows, total = context.repository.list_audit_logs(
        page=page, page_size=page_size, start=_aware(start), end=_aware(end),
        username=username, action=action, outcome=outcome,
    )
    return _page(rows, total, page, page_size, audit_public)


@router.get("/analyses")
async def analysis_logs(
    _: AdminUser, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    start: datetime | None = None, end: datetime | None = None,
    camera_id: str | None = None, mode: str | None = None, status: str | None = None,
) -> dict[str, Any]:
    rows, total = context.repository.list_analysis_logs(
        page=page, page_size=page_size, start=_aware(start), end=_aware(end),
        camera_id=camera_id, mode=mode, status=status,
    )
    return _page(rows, total, page, page_size, analysis_public)


@router.get("/model-calls")
async def model_call_logs(
    _: AdminUser, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    start: datetime | None = None, end: datetime | None = None,
    camera_id: str | None = None, model: str | None = None,
    stage: str | None = None, outcome: str | None = None,
) -> dict[str, Any]:
    rows, total = context.repository.list_model_call_logs(
        page=page, page_size=page_size, start=_aware(start), end=_aware(end),
        camera_id=camera_id, model=model, stage=stage, outcome=outcome,
    )
    return _page(rows, total, page, page_size, model_call_public)


@router.get("/{category}/{row_id}")
async def log_detail(category: str, row_id: int, _: AdminUser) -> dict[str, Any]:
    row = context.repository.get_log(category, row_id)
    if not row:
        raise HTTPException(status_code=404, detail="日志不存在")
    if category == "audit":
        return audit_public(row, True)
    if category == "analyses":
        return analysis_public(row)
    if category == "model-calls":
        return model_call_public(row, True)
    raise HTTPException(status_code=404, detail="日志分类不存在")


def _export_rows(category: str, filters: dict[str, Any]):
    common = {"start": _aware(filters.get("start")), "end": _aware(filters.get("end"))}

    def all_pages(fetch, **kwargs):
        items, page = [], 1
        while True:
            rows, total = fetch(page=page, page_size=1000, **common, **kwargs)
            items.extend(rows)
            if len(items) >= total or not rows:
                return items
            page += 1

    if category == "audit":
        return all_pages(context.repository.list_audit_logs,
                         username=filters.get("username"), action=filters.get("action"),
                         outcome=filters.get("outcome")), audit_public
    if category == "analyses":
        return all_pages(context.repository.list_analysis_logs,
                         camera_id=filters.get("camera_id"), mode=filters.get("mode"),
                         status=filters.get("status")), analysis_public
    if category == "model-calls":
        return all_pages(context.repository.list_model_call_logs,
                         camera_id=filters.get("camera_id"), model=filters.get("model"),
                         stage=filters.get("stage"), outcome=filters.get("outcome")), model_call_public
    raise HTTPException(status_code=404, detail="日志分类不存在")


def _csv_cell(value: Any) -> Any:
    rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
    if isinstance(rendered, str) and rendered.startswith(("=", "+", "-", "@")):
        return "'" + rendered
    return rendered


@router.post("/{category}/export")
async def export_logs(category: str, filters: dict[str, Any], _: AdminUser) -> StreamingResponse:
    for key in ("start", "end"):
        if isinstance(filters.get(key), str) and filters[key]:
            try:
                filters[key] = datetime.fromisoformat(filters[key].replace("Z", "+00:00"))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"{key} 时间格式错误") from exc
    rows, presenter = _export_rows(category, filters)
    rendered = [presenter(row, True) if category in {"audit", "model-calls"} else presenter(row)
                for row in rows]
    output = io.StringIO()
    if rendered:
        writer = csv.DictWriter(output, fieldnames=list(rendered[0]), extrasaction="ignore")
        writer.writeheader()
        for item in rendered:
            writer.writerow({key: _csv_cell(value) for key, value in item.items()})
    data = ("\ufeff" + output.getvalue()).encode("utf-8")
    return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{category}-logs.csv"'
    })
