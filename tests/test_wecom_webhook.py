import base64
import hashlib
import json
from types import SimpleNamespace

import cv2
import httpx
import numpy as np
import pytest

from backend.alerts import AlertService
from backend.config import Settings
from backend.webhook import (
    MAX_WECOM_IMAGE_BYTES,
    WebhookClient,
    build_alert_markdown,
    prepare_wecom_image,
)


def _payload():
    return {
        "camera_name": "一楼 *入口*",
        "camera_id": "camera-1",
        "mode": "intrusion",
        "confidence": 0.956,
        "severity": "critical",
        "zone_name": "仓库_A",
        "reason": "发现 <人员>\n进入",
        "created_at": "2026-09-02T10:20:30+08:00",
    }


def test_alert_markdown_contains_fields_and_escapes_dynamic_markdown():
    content = build_alert_markdown(_payload())
    assert "紧急" in content
    assert "区域闯入" in content
    assert "95.6%" in content
    assert "一楼 \\*入口\\*" in content
    assert "仓库\\_A" in content
    assert "&lt;人员&gt; 进入" in content


def test_large_image_is_compressed_below_wecom_limit(tmp_path):
    random_image = np.random.default_rng(42).integers(0, 256, (1800, 1800, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", random_image, [cv2.IMWRITE_JPEG_QUALITY, 100])
    assert ok and len(encoded) > MAX_WECOM_IMAGE_BYTES
    image_path = tmp_path / "large.jpg"
    image_path.write_bytes(encoded.tobytes())
    result = prepare_wecom_image(image_path)
    assert 0 < len(result) <= MAX_WECOM_IMAGE_BYTES
    assert cv2.imdecode(np.frombuffer(result, dtype=np.uint8), cv2.IMREAD_COLOR) is not None


@pytest.mark.asyncio
async def test_wecom_sends_markdown_then_image_with_required_digest(tmp_path):
    ok, encoded = cv2.imencode(".jpg", np.zeros((8, 8, 3), dtype=np.uint8))
    assert ok
    image_bytes = encoded.tobytes()
    image_path = tmp_path / "evidence.jpg"
    image_path.write_bytes(image_bytes)
    messages = []

    def handler(request: httpx.Request) -> httpx.Response:
        messages.append(json.loads(request.content))
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = WebhookClient(http_client)
    try:
        await client.send("https://example.com/hook", _payload(), image_path, attempts=1)
    finally:
        await http_client.aclose()

    assert [message["msgtype"] for message in messages] == ["markdown", "image"]
    assert base64.b64decode(messages[1]["image"]["base64"]) == image_bytes
    assert messages[1]["image"]["md5"] == hashlib.md5(
        image_bytes, usedforsecurity=False
    ).hexdigest()


@pytest.mark.asyncio
async def test_wecom_retries_nonzero_errcode():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"errcode": 93000, "errmsg": "robot unavailable"})
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = WebhookClient(http_client)
    try:
        await client.send_markdown("https://example.com/hook", "test", attempts=2)
    finally:
        await http_client.aclose()
    assert calls == 2


@pytest.mark.asyncio
async def test_image_failure_is_reported_after_markdown_succeeds(tmp_path):
    messages = []

    def handler(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        messages.append(message["msgtype"])
        if message["msgtype"] == "image":
            return httpx.Response(200, json={"errcode": 40009, "errmsg": "invalid image"})
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    image_path = tmp_path / "evidence.jpg"
    ok, encoded = cv2.imencode(".jpg", np.zeros((8, 8, 3), dtype=np.uint8))
    assert ok
    image_path.write_bytes(encoded.tobytes())
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = WebhookClient(http_client)
    try:
        with pytest.raises(RuntimeError, match="invalid image"):
            await client.send("https://example.com/hook", _payload(), image_path, attempts=1)
    finally:
        await http_client.aclose()
    assert messages == ["markdown", "image"]


@pytest.mark.asyncio
async def test_alert_delivery_records_image_failure(tmp_path):
    class FailingWebhook:
        async def send(self, *_args, **_kwargs):
            raise RuntimeError("企业微信图片发送失败")

    class FakeRepository:
        def __init__(self):
            self.delivery = SimpleNamespace(status="pending")
            self.delivery_error = None
            self.alert_status = None

        def update_webhook_delivery(self, _delivery_id, status, error=None):
            self.delivery.status = status
            self.delivery_error = error

        def webhook_deliveries(self, _alert_id):
            return [self.delivery]

        def update_alert_webhook(self, _alert_id, status):
            self.alert_status = status

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "alert.jpg").write_bytes(b"unused")
    repository = FakeRepository()
    service = AlertService(
        Settings(evidence_dir=evidence_dir), repository, None, None, FailingWebhook()
    )
    await service._deliver(
        1, 2, "https://example.com/hook", {"evidence_url": "/evidence/alert.jpg"}
    )
    assert repository.delivery.status == "failed"
    assert "图片发送失败" in repository.delivery_error
    assert repository.alert_status == "failed"

