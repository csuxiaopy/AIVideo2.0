from __future__ import annotations

import asyncio
import logging
from datetime import timedelta, timezone
from pathlib import Path
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

    @staticmethod
    def _cooldown_active(last, seconds: int) -> bool:
        if not last:
            return False
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return utc_now() - last < timedelta(seconds=seconds)

    async def create(
        self,
        camera: models.Camera,
        analysis: models.Analysis,
        jpeg: bytes,
        bypass_cooldown: bool = False,
        event_phase: str | None = None,
        event_started_at=None,
        event_ended_at=None,
        directory_cooldown_seconds: int | None = None,
    ) -> models.Alert | None:
        options = CameraOptions.model_validate(from_json(camera.options_json, {}))
        cooldown_seconds = options.alert_cooldown_seconds
        if analysis.mode == Mode.FIRE_SMOKE.value:
            cooldown_seconds = 60
        elif analysis.mode == Mode.INTRUSION.value:
            cooldown_seconds = options.intrusion_cooldown_seconds

        directory_id, alert_name = self._directory_subject(camera)
        last = (
            self.repository.latest_directory_alert_time(directory_id, analysis.mode)
            if directory_cooldown_seconds is not None
            else self.repository.latest_alert_time(camera.id, analysis.mode)
        )
        if directory_cooldown_seconds is not None:
            cooldown_seconds = directory_cooldown_seconds
        if not bypass_cooldown and self._cooldown_active(last, cooldown_seconds):
            return None

        reason = f"{analysis.reason}；监控源：{camera.name}"
        return await self._persist(
            camera=camera,
            analysis=analysis,
            directory_id=directory_id,
            alert_name=alert_name,
            reason=reason,
            confidence=analysis.confidence,
            evidence_items=[{
                "camera_id": camera.id,
                "camera_name": camera.name,
                "confidence": analysis.confidence,
                "jpeg": jpeg,
            }],
            event_phase=event_phase,
            event_started_at=event_started_at,
            event_ended_at=event_ended_at,
        )

    async def create_group(
        self,
        camera: models.Camera,
        analysis: models.Analysis,
        evidence_items: list[dict[str, Any]],
        reason: str,
        confidence: float,
        cooldown_seconds: int,
        event_phase: str | None = None,
        event_started_at=None,
        event_ended_at=None,
    ) -> models.Alert | None:
        directory_id, alert_name = self._directory_subject(camera)
        last = self.repository.latest_directory_alert_time(directory_id, analysis.mode)
        if self._cooldown_active(last, cooldown_seconds):
            return None
        return await self._persist(
            camera=camera,
            analysis=analysis,
            directory_id=directory_id,
            alert_name=alert_name,
            reason=reason,
            confidence=confidence,
            evidence_items=evidence_items,
            event_phase=event_phase,
            event_started_at=event_started_at,
            event_ended_at=event_ended_at,
        )

    def _directory_subject(self, camera: models.Camera) -> tuple[int | None, str]:
        directory_id = getattr(camera, "directory_id", None)
        directory = self.repository.get_camera_directory(directory_id) if directory_id is not None else None
        if directory:
            return directory_id, f"{directory.name}营业厅视频"
        return None, "未分组营业厅视频"

    async def _persist(
        self,
        *,
        camera: models.Camera,
        analysis: models.Analysis,
        directory_id: int | None,
        alert_name: str,
        reason: str,
        confidence: float,
        evidence_items: list[dict[str, Any]],
        event_phase: str | None,
        event_started_at,
        event_ended_at,
    ) -> models.Alert:
        created = utc_now()
        filenames: list[str] = []
        evidence_rows: list[dict[str, Any]] = []
        try:
            for index, item in enumerate(evidence_items):
                filename = (
                    f"{item['camera_id']}-{analysis.mode}-"
                    f"{created.strftime('%Y%m%dT%H%M%S%fZ')}-{index}.jpg"
                )
                (self.settings.evidence_dir / filename).write_bytes(item["jpeg"])
                filenames.append(filename)
                evidence_rows.append({
                    "camera_id": item["camera_id"],
                    "camera_name": item["camera_name"],
                    "evidence_path": filename,
                    "confidence": float(item.get("confidence", 0.0)),
                    "sort_order": index,
                    "created_at": created,
                })
            alert = self.repository.add_alert(
                evidences=evidence_rows,
                camera_id=camera.id,
                directory_id=directory_id,
                alert_name=alert_name,
                analysis_id=analysis.id,
                mode=analysis.mode,
                status="confirmed",
                confidence=confidence,
                reason=reason,
                severity=analysis.severity,
                zone_name=analysis.zone_name,
                local_model=analysis.local_model,
                model_version=analysis.model_version,
                evidence_path=filenames[0] if filenames else None,
                webhook_status="not_sent",
                shadow=False,
                event_phase=event_phase,
                event_started_at=event_started_at,
                event_ended_at=event_ended_at,
                created_at=created,
            )
        except Exception:
            for filename in filenames:
                try:
                    (self.settings.evidence_dir / filename).unlink(missing_ok=True)
                except OSError:
                    logger.warning("Failed to roll back evidence file %s", filename)
            raise
        payload = self._payload(alert)
        await self.event_bus.publish(payload)
        self._schedule_delivery(alert.id, payload)
        return alert

    def _payload(self, alert: models.Alert) -> dict[str, Any]:
        evidences = list(getattr(alert, "evidences", []) or [])
        if not evidences and alert.evidence_path:
            evidences = [type("LegacyEvidence", (), {
                "camera_id": alert.camera_id,
                "camera_name": alert.camera_id,
                "evidence_path": alert.evidence_path,
                "confidence": alert.confidence,
            })()]
        evidence_payload = [{
            "camera_id": item.camera_id,
            "camera_name": item.camera_name,
            "confidence": item.confidence,
            "evidence_url": f"/evidence/{item.evidence_path}",
        } for item in evidences]
        first_url = evidence_payload[0]["evidence_url"] if evidence_payload else None
        return {
            "type": "alert",
            "id": alert.id,
            "camera_id": alert.camera_id,
            "camera_name": alert.alert_name or alert.camera_id,
            "alert_name": alert.alert_name or alert.camera_id,
            "directory_id": alert.directory_id,
            "source_cameras": [{
                "camera_id": item["camera_id"], "camera_name": item["camera_name"]
            } for item in evidence_payload],
            "mode": alert.mode,
            "status": alert.status,
            "confidence": alert.confidence,
            "severity": alert.severity,
            "zone_name": alert.zone_name,
            "fire_smoke_class": alert.zone_name if alert.mode == Mode.FIRE_SMOKE.value else None,
            "reason": alert.reason,
            "created_at": alert.created_at.isoformat(),
            "evidence_url": first_url,
            "evidence_urls": [item["evidence_url"] for item in evidence_payload],
            "evidences": evidence_payload,
            "shadow": alert.shadow,
            "event_phase": getattr(alert, "event_phase", None),
            "event_started_at": getattr(alert, "event_started_at", None).isoformat() if getattr(alert, "event_started_at", None) else None,
            "event_ended_at": getattr(alert, "event_ended_at", None).isoformat() if getattr(alert, "event_ended_at", None) else None,
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
            alert_payloads.append((alert, self._payload(alert)))
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
            filenames = [str(item).rsplit("/", 1)[-1] for item in payload.get("evidence_urls", [])]
            if not filenames and payload.get("evidence_url"):
                filenames = [str(payload["evidence_url"]).rsplit("/", 1)[-1]]
            evidence_paths: list[Path] = []
            for filename in filenames:
                evidence_path = (evidence_dir / filename).resolve()
                evidence_path.relative_to(evidence_dir)
                evidence_paths.append(evidence_path)
            await self.webhook.send(url, payload, evidence_paths)
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
