import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from backend.audit import audit_http_request
from backend.api.context import context
from backend.auth import current_user
from backend.main import create_app
from backend.database import utc_now
from backend.schemas import Mode, RetentionSettingsUpdate
from backend.security import token_hash
from backend.vlm import VisionModelClient


def test_log_retention_defaults_to_thirty_days():
    assert RetentionSettingsUpdate().log_retention_days == 30


def test_model_call_log_contains_raw_and_parsed_response_but_not_image():
    records = []
    payload = {
        "id": "request-1",
        "choices": [{"message": {"content": json.dumps({
            "results": [{"mode": "phone_use", "status": "confirmed", "confidence": 0.9,
                         "evidence_frames": [0], "reason": "明确操作手机", "need_review": False}]
        }, ensure_ascii=False)} }],
        "usage": {"total_tokens": 12},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    client = VisionModelClient("https://model.test/v1", "secret", "economy",
                               log_writer=lambda **values: records.append(values))
    original = client.client
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    asyncio.run(original.aclose())
    try:
        result = asyncio.run(client.analyze_behaviors(
            {Mode.PHONE_USE}, b"jpeg-secret", camera_id="camera-1", camera_name="前台"
        ))
    finally:
        asyncio.run(client.close())

    assert result.results[Mode.PHONE_USE].status == "confirmed"
    assert records[0]["outcome"] == "success"
    assert records[0]["stage"] == "single"
    assert records[0]["model"] == "economy"
    assert json.loads(records[0]["parsed_response_json"])["results"][0]["mode"] == "phone_use"
    assert "data:image" not in records[0]["raw_response"]
    assert "secret" not in records[0]["raw_response"]


def test_write_audit_records_authenticated_actor_without_sensitive_body(monkeypatch):
    rows = []
    user = SimpleNamespace(id=7, username="admin", display_name="管理员", enabled=True)
    session = SimpleNamespace(
        id=1, token_hash=token_hash("raw-token"), csrf_token="csrf",
        expires_at=utc_now() + timedelta(hours=1),
    )

    class FakeRepository:
        def get_user_session(self, digest):
            return (session, user) if digest == token_hash("raw-token") else None

        def add_audit_log(self, **values):
            rows.append(values)

    monkeypatch.setattr(context, "repository", FakeRepository())
    request = Request({
        "type": "http", "method": "POST", "path": "/api/cameras", "query_string": b"",
        "headers": [(b"cookie", b"monitor_session=raw-token"), (b"user-agent", b"pytest")],
        "client": ("127.0.0.1", 1234), "scheme": "http", "server": ("test", 80),
    })

    async def call_next(_):
        return Response(status_code=201)

    asyncio.run(audit_http_request(request, call_next))
    assert rows[0]["actor_username"] == "admin"
    assert rows[0]["outcome"] == "success"
    assert "password" not in rows[0]["summary_json"]


def test_log_api_rejects_non_admin_user():
    app = create_app()
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(
        id=9, username="operator", display_name="值班员", role="user", enabled=True
    )
    try:
        response = TestClient(app).get("/api/logs/audit")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 403
