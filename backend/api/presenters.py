from __future__ import annotations

from typing import Any

from backend import models
from backend.repository import from_json
from backend.security import SecretCipher, redact_rtsp


def camera_public(
    camera: models.Camera,
    cipher: SecretCipher,
    preview_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": camera.id,
        "name": camera.name,
        "enabled": camera.enabled,
        "scene_type": camera.scene_type,
        "source": redact_rtsp(cipher.decrypt(camera.rtsp_url_encrypted)),
        "online": camera.online,
        "camera_online": camera.online,
        "last_seen_at": camera.last_seen_at,
        "last_frame_at": camera.last_frame_at,
        "last_analysis_at": camera.last_analysis_at,
        "frame_interval_seconds": camera.frame_interval_seconds,
        "last_error": camera.last_error,
        "preview_active": bool(preview_status and preview_status.get("active")),
        "preview_started_at": preview_status.get("started_at") if preview_status else None,
        "modes": from_json(camera.modes_json, []),
        "geometry": from_json(camera.geometry_json, {}),
        "schedule": from_json(camera.schedule_json, {}),
        "options": from_json(camera.options_json, {}),
        "created_at": camera.created_at,
        "updated_at": camera.updated_at,
    }


def alert_public(alert: models.Alert) -> dict[str, Any]:
    return {
        "id": alert.id,
        "camera_id": alert.camera_id,
        "analysis_id": alert.analysis_id,
        "mode": alert.mode,
        "status": alert.status,
        "confidence": alert.confidence,
        "severity": alert.severity,
        "zone_name": alert.zone_name,
        "local_model": alert.local_model,
        "model_version": alert.model_version,
        "reason": alert.reason,
        "evidence_path": alert.evidence_path,
        "evidence_url": f"/evidence/{alert.evidence_path}" if alert.evidence_path else None,
        "webhook_status": alert.webhook_status,
        "shadow": alert.shadow,
        "created_at": alert.created_at,
    }


def analysis_public(row: models.Analysis) -> dict[str, Any]:
    return {
        "id": row.id,
        "camera_id": row.camera_id,
        "mode": row.mode,
        "status": row.status,
        "confidence": row.confidence,
        "reason": row.reason,
        "evidence_path": row.evidence_path,
        "request_id": row.request_id,
        "provider": row.provider,
        "model": row.model,
        "severity": row.severity,
        "zone_name": row.zone_name,
        "local_model": row.local_model,
        "model_version": row.model_version,
        "usage": from_json(row.usage_json, {}),
        "error": row.error,
        "latency_ms": row.latency_ms,
        "created_at": row.created_at,
    }
