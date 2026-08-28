from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from backend.api.context import context
from backend.schemas import DetectorSettingsUpdate, ModelSettingsUpdate, WebhookSettingsUpdate
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
        "general_model": row.general_model,
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


@router.get("/webhook")
async def get_webhook() -> dict[str, Any]:
    row = context.repository.get_webhook_settings()
    return {
        "enabled": row.enabled,
        "url": row.url,
        "secret_configured": bool(row.secret_encrypted),
        "updated_at": row.updated_at,
    }


@router.put("/webhook")
async def put_webhook(payload: WebhookSettingsUpdate) -> dict[str, Any]:
    existing = context.repository.get_webhook_settings()
    effective_secret = payload.secret or (
        context.cipher.decrypt(existing.secret_encrypted) if existing.secret_encrypted else ""
    )
    if payload.enabled and (not payload.url.startswith("https://") or not effective_secret):
        raise HTTPException(status_code=400, detail="启用 Webhook 时必须配置 HTTPS URL 和密钥")
    values: dict[str, Any] = {"enabled": payload.enabled, "url": payload.url}
    if payload.secret:
        values["secret_encrypted"] = context.cipher.encrypt(payload.secret)
    context.repository.save_webhook_settings(values)
    return await get_webhook()


@router.post("/webhook/test")
async def test_webhook() -> dict[str, Any]:
    row = context.repository.get_webhook_settings()
    if not row.url or not row.secret_encrypted:
        raise HTTPException(status_code=400, detail="Webhook 尚未完整配置")
    client = WebhookClient()
    try:
        await client.send(
            row.url,
            context.cipher.decrypt(row.secret_encrypted),
            {"type": "test", "message": "YOLO VLM monitor webhook test"},
            attempts=1,
        )
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.close()
