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
            webhook_status="not_sent",
            shadow=False,
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
        targets = [target for target in self.repository.list_webhook_targets(enabled_only=True)
                   if payload["severity"] in from_json(target.auto_severities_json, [])
                   and target.url]
        for target in targets:
            delivery = self.repository.upsert_webhook_delivery(alert_id, target, "automatic")
            asyncio.create_task(
                self._deliver(alert_id, delivery.id, target.url, payload),
                name=f"alert-webhook-{alert_id}-{target.id}",
            )
        if targets:
            self._refresh_alert_status(alert_id)

    async def manual_send(self, alert_ids: list[int], target_ids: list[int]) -> dict[str, int]:
        targets = []
        for target_id in target_ids:
            target = self.repository.get_webhook_target(target_id)
            if not target or not target.enabled or not target.url:
                raise ValueError(f"Webhook 目标 {target_id} 不存在、未启用或配置不完整")
            targets.append(target)
        alert_payloads = []
        for alert_id in alert_ids:
            alert = self.repository.get_alert(alert_id)
            if not alert:
                raise ValueError(f"告警 {alert_id} 不存在")
            camera = self.repository.get_camera(alert.camera_id)
            if not camera:
                raise ValueError(f"告警 {alert_id} 对应摄像头不存在")
            alert_payloads.append((alert, self._payload(camera, alert, alert.evidence_path or "")))
        jobs = []
        for alert, payload in alert_payloads:
            for target in targets:
                delivery = self.repository.upsert_webhook_delivery(alert.id, target, "manual")
                jobs.append(self._deliver(alert.id, delivery.id, target.url, payload))
        await asyncio.gather(*jobs)
        return {"alerts": len(alert_ids), "targets": len(targets), "deliveries": len(jobs)}

    async def _deliver(
        self, alert_id: int, delivery_id: int, url: str, payload: dict[str, Any]
    ) -> None:
        try:
            evidence_dir = self.settings.evidence_dir.resolve()
            filename = str(payload.get("evidence_url", "")).rsplit("/", 1)[-1]
            evidence_path = (evidence_dir / filename).resolve()
            evidence_path.relative_to(evidence_dir)
            await self.webhook.send(url, payload, evidence_path)
            self.repository.update_webhook_delivery(delivery_id, "delivered")
        except Exception as exc:
            self.repository.update_webhook_delivery(delivery_id, "failed", str(exc)[:1000])
            logger.exception("Webhook delivery failed for alert %s", alert_id)
        self._refresh_alert_status(alert_id)

    def _refresh_alert_status(self, alert_id: int) -> None:
        rows = self.repository.webhook_deliveries(alert_id)
        delivered = sum(row.status == "delivered" for row in rows)
        failed = sum(row.status == "failed" for row in rows)
        if not rows:
            status = "not_sent"
        elif delivered == len(rows):
            status = "delivered"
        elif failed == len(rows):
            status = "failed"
        elif delivered and failed:
            status = "partial"
        else:
            status = "pending"
        self.repository.update_alert_webhook(alert_id, status)
