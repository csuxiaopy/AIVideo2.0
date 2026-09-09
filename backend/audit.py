from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

from backend.api.context import context
from backend.auth import SESSION_COOKIE, resolve_session
from backend.repository import as_json


logger = logging.getLogger(__name__)
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _operation(method: str, path: str) -> tuple[str, str, str]:
    parts = [part for part in path.split("/") if part]
    resource = parts[1] if len(parts) > 1 and parts[0] == "api" else (parts[0] if parts else "system")
    target_id = parts[2] if len(parts) > 2 and parts[0] == "api" else ""
    special = {
        "/api/auth/login": "用户登录",
        "/api/auth/logout": "用户退出",
        "/api/auth/password": "修改本人密码",
        "/api/cameras/batch": "批量新增摄像头",
        "/api/cameras/batch-delete": "批量删除摄像头",
        "/api/cameras/batch-move": "批量移动摄像头",
        "/api/cameras/batch-off-duty-schedule": "批量配置离岗排班",
        "/api/alerts/export": "导出告警",
        "/api/alerts/webhook-send": "发送告警 Webhook",
    }
    if path in special:
        return special[path], resource, target_id
    if path.startswith("/api/logs/") and path.endswith("/export"):
        return "导出系统日志", "logs", parts[2] if len(parts) > 2 else ""
    if resource == "cameras" and path.endswith("/analyze"):
        return "即时分析摄像头", resource, target_id
    if resource == "cameras" and "/preview/" in path:
        return ("开启实时预览" if path.endswith("/start") else "停止实时预览"), resource, target_id
    verb = {"POST": "新增/执行", "PUT": "更新", "PATCH": "修改", "DELETE": "删除"}.get(method, method)
    return f"{verb}{resource}", resource, target_id


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    return forwarded or (request.client.host if request.client else "")


async def audit_http_request(request: Request, call_next):
    if request.method not in WRITE_METHODS or not request.url.path.startswith("/api/"):
        return await call_next(request)
    if request.url.path.endswith("/preview/heartbeat"):
        return await call_next(request)

    actor = None
    try:
        found = resolve_session(request.cookies.get(SESSION_COOKIE), touch=False)
        actor = found[1] if found else None
    except Exception:
        logger.exception("Unable to resolve audit actor")

    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        actor = getattr(request.state, "audit_user", None) or actor
        attempted_username = str(getattr(request.state, "audit_username", ""))[:64]
        is_login = request.url.path == "/api/auth/login"
        if actor is not None or is_login:
            action, target_type, target_id = _operation(request.method, request.url.path)
            summary: dict[str, Any] = {
                "result": "成功" if status_code < 400 else "失败",
                "status_code": status_code,
            }
            if is_login and not actor:
                summary["attempted_username"] = attempted_username
            try:
                context.repository.add_audit_log(
                    actor_user_id=getattr(actor, "id", None),
                    actor_username=getattr(actor, "username", "") or attempted_username,
                    actor_display_name=getattr(actor, "display_name", ""),
                    action=action,
                    target_type=str(getattr(request.state, "audit_target_type", target_type))[:80],
                    target_id=str(getattr(request.state, "audit_target_id", target_id))[:200],
                    method=request.method,
                    path=request.url.path[:500],
                    outcome="success" if status_code < 400 else "failure",
                    status_code=status_code,
                    summary_json=as_json(summary),
                    ip_address=_client_ip(request)[:100],
                    user_agent=request.headers.get("user-agent", "")[:500],
                )
            except Exception:
                logger.exception("Failed to persist audit log for %s %s", request.method, request.url.path)
