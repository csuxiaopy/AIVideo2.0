from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from backend.api.context import context
from backend.repository import as_json, from_json
from backend.schemas import (DisplaySettingsUpdate, DetectorSettingsUpdate, ModelSettingsUpdate,
    RetentionSettingsUpdate, WebhookTargetCreate, WebhookTargetUpdate)
from backend.webhook import WebhookClient


router = APIRouter(prefix="/api/settings")


@router.get("/models")
async def get_models() -> dict[str, Any]:
    row = context.repository.get_model_settings()
    return {
        "provider": row.provider,
        "base_url": row.base_url,
        "economy_model": row.economy_model,
        "enhanced_model": row.enhanced_model,
        "api_key_configured": bool(row.api_key_encrypted),
        "updated_at": row.updated_at,
    }


@router.put("/models")
async def put_models(payload: ModelSettingsUpdate) -> dict[str, Any]:
    existing = context.repository.get_model_settings()
    effective_key = payload.api_key or (
        context.cipher.decrypt(existing.api_key_encrypted) if existing.api_key_encrypted else ""
    )
    if payload.provider != "mock" and (not payload.base_url or not effective_key):
        raise HTTPException(status_code=400, detail="外部模型必须配置 Base URL 和 API Key")
    values = {
        "provider": payload.provider,
        "base_url": payload.base_url,
        "economy_model": payload.economy_model,
        "enhanced_model": payload.enhanced_model,
    }
    if payload.api_key:
        values["api_key_encrypted"] = context.cipher.encrypt(payload.api_key)
    context.repository.save_model_settings(values)
    await context.require_runtime().reload_models()
    return await get_models()


@router.post("/models/test")
async def test_models() -> dict[str, Any]:
    row = context.repository.get_model_settings()
    if row.provider == "mock":
        return {"ok": True, "provider": "mock", "latency_ms": 0}
    vlm = context.require_runtime().vlm
    if not vlm:
        raise HTTPException(status_code=400, detail="视觉大模型尚未完整配置")
    try:
        return await vlm.test()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/detectors")
async def get_detectors() -> dict[str, Any]:
    row = context.repository.get_detector_settings()
    runtime_status = await context.require_runtime().status()
    return {
        "general_model": runtime_status["detectors"]["general"].get("model", row.general_model),
        "general_device": row.general_device,
        "fire_smoke_model": row.fire_smoke_model,
        "fire_smoke_device": row.fire_smoke_device,
        "model_sha256": row.model_sha256,
        "license_name": row.license_name,
        "updated_at": row.updated_at,
        "runtime": runtime_status["detectors"],
    }


@router.put("/detectors")
async def put_detectors(payload: DetectorSettingsUpdate) -> dict[str, Any]:
    context.repository.save_detector_settings(payload.model_dump())
    await context.require_runtime().reload_detectors()
    return await get_detectors()


def webhook_target_public(row) -> dict[str, Any]:
    return {
        "id": row.id, "name": row.name, "enabled": row.enabled, "url": row.url,
        # Compatibility for cached pre-WeCom frontends that required an HMAC secret.
        # WeCom robot URLs contain their own key and do not use a separate secret.
        "secret_configured": bool(row.url),
        "auto_severities": from_json(row.auto_severities_json, []),
        "created_at": row.created_at, "updated_at": row.updated_at,
    }


@router.get("/webhooks")
async def get_webhooks() -> dict[str, Any]:
    return {
        "items": [webhook_target_public(row) for row in context.repository.list_webhook_targets()],
    }


@router.post("/webhooks", status_code=201)
async def create_webhook(payload: WebhookTargetCreate) -> dict[str, Any]:
    if payload.enabled and not payload.url.startswith("https://"):
        raise HTTPException(status_code=400, detail="启用企业微信机器人时必须配置 HTTPS URL")
    values = payload.model_dump(exclude={"auto_severities"})
    values["secret_encrypted"] = ""
    values["auto_severities_json"] = as_json(payload.auto_severities)
    return webhook_target_public(context.repository.create_webhook_target(values))


@router.put("/webhooks/{target_id}")
async def update_webhook(target_id: int, payload: WebhookTargetUpdate) -> dict[str, Any]:
    existing = context.repository.get_webhook_target(target_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Webhook 目标不存在")
    if payload.enabled and not payload.url.startswith("https://"):
        raise HTTPException(status_code=400, detail="启用企业微信机器人时必须配置 HTTPS URL")
    values = payload.model_dump(exclude={"auto_severities"})
    values["auto_severities_json"] = as_json(payload.auto_severities)
    row = context.repository.update_webhook_target(target_id, values)
    return webhook_target_public(row)


@router.delete("/webhooks/{target_id}")
async def delete_webhook(target_id: int) -> dict[str, bool]:
    if not context.repository.delete_webhook_target(target_id):
        raise HTTPException(status_code=404, detail="Webhook 目标不存在")
    return {"deleted": True}


@router.post("/webhooks/{target_id}/test")
async def test_webhook_target(target_id: int) -> dict[str, bool]:
    row = context.repository.get_webhook_target(target_id)
    if not row or not row.url:
        raise HTTPException(status_code=400, detail="Webhook 目标不存在或配置不完整")
    client = WebhookClient()
    try:
        await client.send_markdown(
            row.url, "### AI 视频监控\n> 企业微信机器人连接测试成功", attempts=1
        )
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.close()


@router.get("/retention")
async def get_retention() -> dict[str, Any]:
    row = context.repository.get_retention_settings()
    return {
        "alert_retention_days": row.alert_retention_days,
        "auto_cleanup_enabled": row.auto_cleanup_enabled,
        "updated_at": row.updated_at,
    }


@router.put("/retention")
async def put_retention(payload: RetentionSettingsUpdate) -> dict[str, Any]:
    context.repository.save_retention_settings(payload.model_dump())
    return await get_retention()


@router.get("/display")
async def get_display() -> dict[str, Any]:
    row = context.repository.get_display_settings()
    return {
        "show_traffic_report": row.show_traffic_report,
        "show_current_store_count": row.show_current_store_count,
        "updated_at": row.updated_at,
    }


@router.patch("/display")
async def patch_display(payload: DisplaySettingsUpdate) -> dict[str, Any]:
    values = payload.model_dump(exclude_none=True)
    if values:
        context.repository.save_display_settings(values)
    return await get_display()
