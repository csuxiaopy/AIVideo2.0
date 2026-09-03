from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError

from backend import models
from backend.api.context import context
from backend.auth import AdminUser, CurrentUser, SESSION_COOKIE, user_public
from backend.database import utc_now
from backend.schemas import LoginRequest, PasswordChange, PasswordReset, UserCreate, UserUpdate
from backend.security import hash_password, random_token, token_hash, validate_username, verify_password

router = APIRouter(prefix="/api")


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                        secure=context.settings.secure_cookies,
                        max_age=context.settings.session_idle_hours * 3600, path="/")


@router.post("/auth/login")
async def login(payload: LoginRequest, response: Response) -> dict:
    user = context.repository.get_user_by_username(payload.username.strip())
    if not user or not user.enabled or not verify_password(user.password_hash, payload.password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    raw, csrf = random_token(), random_token()
    now = utc_now()
    context.repository.create_user_session(models.UserSession(
        user_id=user.id, token_hash=token_hash(raw), csrf_token=csrf,
        last_seen_at=now, expires_at=now + timedelta(hours=context.settings.session_idle_hours)))
    _set_cookie(response, raw)
    return {"user": user_public(user), "csrf_token": csrf}


@router.get("/auth/me")
async def me(user: CurrentUser, request: Request) -> dict:
    raw = request.cookies.get(SESSION_COOKIE, "")
    found = context.repository.get_user_session(token_hash(raw))
    return {"user": user_public(user), "csrf_token": found[0].csrf_token if found else ""}


@router.post("/auth/logout")
async def logout(user: CurrentUser, request: Request, response: Response) -> Response:
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        context.repository.delete_user_session(token_hash(raw))
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.put("/auth/password")
async def change_password(payload: PasswordChange, user: CurrentUser, response: Response) -> Response:
    if not verify_password(user.password_hash, payload.old_password):
        raise HTTPException(status_code=400, detail="原密码错误")
    try:
        password_hash = hash_password(payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    context.repository.update_user(user.id, {"password_hash": password_hash})
    context.repository.delete_user_sessions(user.id)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/users")
async def users(_: AdminUser) -> list[dict]:
    return [user_public(row) for row in context.repository.list_users()]


@router.post("/users", status_code=201)
async def create_user(payload: UserCreate, _: AdminUser) -> dict:
    try:
        if context.repository.get_user_by_username(payload.username.strip()):
            raise HTTPException(status_code=409, detail="用户名已存在")
        row = models.User(username=validate_username(payload.username).lower(), display_name=payload.display_name.strip(),
                          password_hash=hash_password(payload.password), role="user", enabled=True)
        return user_public(context.repository.create_user(row))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="用户名已存在") from exc


@router.patch("/users/{user_id}")
async def update_user(user_id: int, payload: UserUpdate, actor: AdminUser) -> dict:
    target = context.repository.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="账号不存在")
    values = payload.model_dump(exclude_none=True)
    if user_id == actor.id and values.get("enabled") is False:
        raise HTTPException(status_code=400, detail="不能停用当前登入账号")
    if "display_name" in values:
        values["display_name"] = values["display_name"].strip()
    updated = context.repository.update_user(user_id, values)
    if values.get("enabled") is False:
        context.repository.delete_user_sessions(user_id)
    return user_public(updated)


@router.post("/users/{user_id}/reset-password", status_code=204)
async def reset_password(user_id: int, payload: PasswordReset, _: AdminUser) -> Response:
    target = context.repository.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="账号不存在")
    try:
        password_hash = hash_password(payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    context.repository.update_user(user_id, {"password_hash": password_hash})
    context.repository.delete_user_sessions(user_id)
    return Response(status_code=204)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: int, actor: AdminUser) -> Response:
    target = context.repository.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="账号不存在")
    if user_id == actor.id:
        raise HTTPException(status_code=400, detail="不能删除当前登入账号")
    if target.role == "admin":
        raise HTTPException(status_code=403, detail="不能通过此接口删除管理员")
    context.repository.delete_user(user_id)
    return Response(status_code=204)
