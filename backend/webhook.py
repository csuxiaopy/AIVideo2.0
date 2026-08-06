from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from backend.security import sign_webhook


class WebhookClient:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=15)

    async def close(self) -> None:
        await self.client.aclose()

    async def send(self, url: str, secret: str, payload: dict[str, Any], attempts: int = 5) -> None:
        last_error: Exception | None = None
        for attempt in range(attempts):
            timestamp = str(int(datetime.now(timezone.utc).timestamp()))
            signature = sign_webhook(secret, timestamp, payload)
            try:
                response = await self.client.post(
                    url,
                    json=payload,
                    headers={"X-Monitor-Timestamp": timestamp, "X-Monitor-Signature": signature},
                )
                response.raise_for_status()
                return
            except Exception as exc:
                last_error = exc
                if attempt < attempts - 1:
                    await asyncio.sleep(min(30, 2 ** attempt))
        raise RuntimeError(f"Webhook 发送失败：{str(last_error)[:300]}")

