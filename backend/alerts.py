from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from backend import models
from backend.config import Settings
from backend.database import utc_now
from backend.eventbus import EventBus
from backend.repository import Repository, from_json
from backend.schemas import CameraOptions, Mode
from backend.security import SecretCipher
from backend.webhook import WebhookClient


logger = logging.getLogger(__name__)


class AlertService:
    """Persists, publishes and delivers confirmed monitoring alerts."""

    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        cipher: SecretCipher,
        event_bus: EventBus,
        webhook: WebhookClient,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.cipher = cipher
        self.event_bus = event_bus
        self.webhook = webhook

    async def create(
        self,
        camera: models.Camera,
        analysis: models.Analysis,
        jpeg: bytes,
        bypass_cooldown: bool = False,
    ) -> None:
        options = CameraOptions.model_validate(from_json(camera.options_json, {}))
        cooldown_seconds = options.alert_cooldown_seconds
        if analysis.mode == Mode.FIRE_SMOKE.value:
            cooldown_seconds = 60
        elif analysis.mode == Mode.INTRUSION.value:
            cooldown_seconds = options.intrusion_cooldown_seconds

        last = self.repository.latest_alert_time(camera.id, analysis.mode)
        if not bypass_cooldown and last and utc_now() - last < timedelta(seconds=cooldown_seconds):
            return

        filename = f"{camera.id}-{analysis.mode}-{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}.jpg"
        (self.settings.evidence_dir / filename).write_bytes(jpeg)
        alert = self.repository.add_alert(
            camera_id=camera.id,
            analysis_id=analysis.id,
            mode=analysis.mode,
            status="confirmed",
            confidence=analysis.confidence,
            reason=analysis.reason,
            severity=analysis.severity,
            zone_name=analysis.zone_name,
            local_model=analysis.local_model,
            model_version=analysis.model_version,
            evidence_path=filename,
            webhook_status="shadow" if self.settings.shadow_mode else "pending",
            shadow=self.settings.shadow_mode,
        )
        payload = self._payload(camera, alert, filename)
        await self.event_bus.publish(payload)
        self._schedule_delivery(alert.id, payload)

    def _payload(
        self, camera: models.Camera, alert: models.Alert, filename: str
    ) -> dict[str, Any]:
        return {
            "type": "alert",
            "id": alert.id,
            "camera_id": camera.id,
            "camera_name": camera.name,
            "mode": alert.mode,
            "status": alert.status,
            "confidence": alert.confidence,
            "severity": alert.severity,
            "scene_type": camera.scene_type,
            "zone_name": alert.zone_name,
            "fire_smoke_class": alert.zone_name if alert.mode == Mode.FIRE_SMOKE.value else None,
            "reason": alert.reason,
            "created_at": alert.created_at.isoformat(),
            "evidence_url": f"/evidence/{filename}",
            "shadow": alert.shadow,
        }

    def _schedule_delivery(self, alert_id: int, payload: dict[str, Any]) -> None:
        if self.settings.shadow_mode:
            return
        webhook_settings = self.repository.get_webhook_settings()
        if (
            webhook_settings.enabled
            and webhook_settings.url
            and webhook_settings.secret_encrypted
        ):
            secret = self.cipher.decrypt(webhook_settings.secret_encrypted)
            asyncio.create_task(
                self._deliver(alert_id, webhook_settings.url, secret, payload),
                name=f"alert-webhook-{alert_id}",
            )

    async def _deliver(
        self, alert_id: int, url: str, secret: str, payload: dict[str, Any]
    ) -> None:
        try:
            await self.webhook.send(url, secret, payload)
            self.repository.update_alert_webhook(alert_id, "delivered")
        except Exception:
            self.repository.update_alert_webhook(alert_id, "failed")
            logger.exception("Webhook delivery failed for alert %s", alert_id)
