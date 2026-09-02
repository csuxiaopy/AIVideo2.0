from __future__ import annotations

import asyncio
import base64
import hashlib
from pathlib import Path
from typing import Any

import cv2
import httpx
import numpy as np


MAX_WECOM_IMAGE_BYTES = 2 * 1024 * 1024
SEVERITY_LABELS = {"normal": "普通", "high": "高", "critical": "紧急"}
SEVERITY_COLORS = {"normal": "info", "high": "warning", "critical": "warning"}
MODE_LABELS = {
    "black_screen": "黑屏", "fire_smoke": "烟火", "intrusion": "区域闯入",
    "off_duty": "离岗", "phone_use": "玩手机", "smoking": "吸烟",
}


def _markdown_text(value: Any) -> str:
    text = " ".join(("" if value is None else str(value)).splitlines())
    for character in ("\\", "`", "*", "_", "[", "]", "#"):
        text = text.replace(character, f"\\{character}")
    return text.replace("<", "&lt;").replace(">", "&gt;")


def build_alert_markdown(payload: dict[str, Any]) -> str:
    severity = str(payload.get("severity") or "normal")
    mode = str(payload.get("mode") or "")
    confidence = payload.get("confidence")
    confidence_text = "-" if confidence is None else f"{float(confidence):.1%}"
    return "\n".join((
        "### AI 视频监控告警",
        f"> 告警级别：<font color=\"{SEVERITY_COLORS.get(severity, 'comment')}\">"
        f"{_markdown_text(SEVERITY_LABELS.get(severity, severity))}</font>",
        f"> 摄像头：{_markdown_text(payload.get('camera_name') or payload.get('camera_id') or '-')}",
        f"> 告警事件：{_markdown_text(MODE_LABELS.get(mode, mode or '-'))}",
        f"> 置信度：{_markdown_text(confidence_text)}",
        f"> 区域：{_markdown_text(payload.get('zone_name') or '-')}",
        f"> 告警原因：{_markdown_text(payload.get('reason') or '-')}",
        f"> 发生时间：{_markdown_text(payload.get('created_at') or '-')}",
    ))


def prepare_wecom_image(image_path: Path) -> bytes:
    try:
        image_bytes = image_path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"证据图片读取失败：{exc}") from exc
    if not image_bytes:
        raise RuntimeError("证据图片为空")
    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError("证据图片损坏，无法发送")
    if len(image_bytes) <= MAX_WECOM_IMAGE_BYTES:
        return image_bytes
    scale = 1.0
    while scale >= 0.25:
        candidate = image if scale == 1.0 else cv2.resize(
            image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
        )
        for quality in (85, 70, 55, 40, 25):
            ok, encoded = cv2.imencode(".jpg", candidate, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if ok and len(encoded) <= MAX_WECOM_IMAGE_BYTES:
                return encoded.tobytes()
        scale *= 0.75
    raise RuntimeError("证据图片压缩后仍超过企业微信 2 MB 限制")


class WebhookClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=15)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _post(self, url: str, message: dict[str, Any], attempts: int) -> None:
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = await self.client.post(url, json=message)
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict) or result.get("errcode") != 0:
                    code = result.get("errcode") if isinstance(result, dict) else "invalid_response"
                    detail = result.get("errmsg", "未知错误") if isinstance(result, dict) else "响应不是 JSON 对象"
                    raise RuntimeError(f"企业微信返回错误 {code}：{detail}")
                return
            except Exception as exc:
                last_error = exc
                if attempt < attempts - 1:
                    await asyncio.sleep(min(30, 2 ** attempt))
        raise RuntimeError(f"Webhook 发送失败：{str(last_error)[:300]}") from last_error

    async def send_markdown(self, url: str, content: str, attempts: int = 5) -> None:
        await self._post(url, {"msgtype": "markdown", "markdown": {"content": content}}, attempts)

    async def send(
        self, url: str, payload: dict[str, Any], evidence_path: Path, attempts: int = 5
    ) -> None:
        await self.send_markdown(url, build_alert_markdown(payload), attempts)
        image_bytes = prepare_wecom_image(evidence_path)
        await self._post(url, {"msgtype": "image", "image": {
            "base64": base64.b64encode(image_bytes).decode("ascii"),
            "md5": hashlib.md5(image_bytes, usedforsecurity=False).hexdigest(),
        }}, attempts)

