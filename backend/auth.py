from __future__ import annotations

from datetime import timedelta, timezone
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request, Response, WebSocket, status

from backend import models
from backend.api.context import context
from backend.database import utc_now
from backend.security import token_hash

SESSION_COOKIE = "monitor_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def user_public(user: models.User) -> dict:
    return {"id": user.id, "username": user.username, "display_name": user.display_name,
            "role": user.role, "enabled": user.enabled, "created_at": user.created_at,
            "updated_at": user.updated_at}


def resolve_session(raw_token: str | None, *, touch: bool = True):
    if not raw_token:
        return None
    found = context.repository.get_user_session(token_hash(raw_token))
    if not found:
        return None
    session, user = found
    now = utc_now()
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if not user.enabled or expires_at <= now:
        context.repository.delete_user_session(session.token_hash)
        return None
    if touch:
        context.repository.touch_user_session(
            session.id, now, now + timedelta(hours=context.settings.session_idle_hours))
    return session, user


async def current_user(
    request: Request,
    response: Response,
    monitor_session: Annotated[str | None, Cookie()] = None,
    csrf: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> models.User:
    found = resolve_session(monitor_session)
    if not found:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登入")
    session, user = found
    if request.method not in SAFE_METHODS and csrf != session.csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 校验失败")
    response.set_cookie(
        SESSION_COOKIE, monitor_session, httponly=True, samesite="lax",
        secure=context.settings.secure_cookies,
        max_age=context.settings.session_idle_hours * 3600, path="/",
    )
    request.state.user = user
    return user


async def admin_user(user: Annotated[models.User, Depends(current_user)]) -> models.User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def websocket_user(websocket: WebSocket) -> models.User | None:
    origin = websocket.headers.get("origin")
    allowed = {item.strip() for item in context.settings.allowed_origins.split(",") if item.strip()}
    if origin and origin not in allowed:
        return None
    found = resolve_session(websocket.cookies.get(SESSION_COOKIE))
    return found[1] if found else None


CurrentUser = Annotated[models.User, Depends(current_user)]
AdminUser = Annotated[models.User, Depends(admin_user)]
