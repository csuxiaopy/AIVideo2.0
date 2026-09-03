from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

from backend.auth import admin_user, current_user, resolve_session
from backend.api.context import context
from backend.database import utc_now
from backend.security import hash_password, token_hash, validate_password, validate_username, verify_password


class FakeRepository:
    def __init__(self, role="user", enabled=True, expired=False):
        now = utc_now()
        self.user = SimpleNamespace(id=7, role=role, enabled=enabled)
        self.session = SimpleNamespace(
            id=9, user_id=7, token_hash=token_hash("secret"), csrf_token="csrf",
            last_seen_at=now, expires_at=now - timedelta(seconds=1) if expired else now + timedelta(hours=8),
        )
        self.touched = False
        self.deleted = False

    def get_user_session(self, digest):
        return (self.session, self.user) if digest == self.session.token_hash else None

    def touch_user_session(self, *_):
        self.touched = True

    def delete_user_session(self, *_):
        self.deleted = True


def request(method="GET"):
    return Request({"type": "http", "method": method, "path": "/api/test", "headers": []})


def test_password_hash_and_identity_validation():
    hashed = hash_password("secure-pass")
    assert "secure-pass" not in hashed
    assert verify_password(hashed, "secure-pass")
    assert not verify_password(hashed, "wrong-pass")
    assert validate_username("operator_01") == "operator_01"
    with pytest.raises(ValueError):
        validate_username("bad name")
    with pytest.raises(ValueError):
        validate_password("short")


@pytest.mark.asyncio
async def test_session_requires_csrf_for_write_and_admin_role(monkeypatch):
    repo = FakeRepository(role="user")
    monkeypatch.setattr(context, "repository", repo)
    user = await current_user(request(), Response(), "secret", None)
    assert user.id == 7 and repo.touched
    with pytest.raises(HTTPException) as csrf_error:
        await current_user(request("POST"), Response(), "secret", None)
    assert csrf_error.value.status_code == 403
    with pytest.raises(HTTPException) as role_error:
        await admin_user(user)
    assert role_error.value.status_code == 403


def test_expired_or_disabled_session_is_revoked(monkeypatch):
    repo = FakeRepository(expired=True)
    monkeypatch.setattr(context, "repository", repo)
    assert resolve_session("secret") is None
    assert repo.deleted
