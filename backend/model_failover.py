"""One analysis-owned coordinator. A continuous 30-minute outage gates switching."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
import re
import time

import httpx

from backend.database import utc_now
from backend.model_probe import validate_model
from backend.model_switch_store import ModelSwitchStore, aware, safe_error
from backend.vlm import VisionModelClient

logger = logging.getLogger(__name__)
OUTAGE_SECONDS = 30 * 60


def classify(reason, status=None):
    text = str(reason).lower()
    match = re.search(r"http\s+(\d{3})", text)
    status = status or (int(match[1]) if match else None)
    if status in {401, 403} or any(word in text for word in ("insufficient_quota", "余额", "billing", "invalid_api_key")):
        return "credentials_or_quota", status
    if status == 429:
        return "rate_limited", status
    if any(word in text for word in ("model_not_found", "model not found", "无可用渠道", "no available channel", "unsupported image", "does not support image")):
        return "model_unavailable", status
    if "格式" in text or "json" in text:
        return "invalid_response", status
    return "upstream_error", status


def rank_candidates(items, current):
    def score(item):
        name = item["id"].lower()
        metadata = str(item.get("input_modalities", item.get("capabilities", ""))).lower()
        tier = 0 if "image" in metadata or "vision" in metadata else 1 if "vision" in name or re.search(r"(^|[-_])vl([-_]|$)", name) else 2
        family = 0 if name.startswith(current.split("-")[0].lower()) else 1
        return tier, family, name
    valid = [item for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)
             and item["id"] != current and 0 < len(item["id"]) <= 200
             and not any(word in item["id"].lower() for word in ("embedding", "rerank", "bge-", "whisper", "tts", "dall-e"))]
    unique = {item["id"]: item for item in valid}
    return [item["id"] for item in sorted(unique.values(), key=score)]


class ModelFailover:
    def __init__(self, client, config, cipher, store=ModelSwitchStore):
        self.client, self.config, self.cipher, self.store = client, config, cipher, store
        self.state = {}
        self.task = None
        self.closed = False
        self.probing = False
        self.dirty = False
        self.revision = 0
        self._round_lock = asyncio.Lock()

    async def start(self):
        self.state = await asyncio.to_thread(self.store.load_state, self.config)
        self.state["reason"] = safe_error(self.state.get("reason", ""), self.client.api_key)
        reason, status = classify(self.state.get("reason", ""), self.state.get("http_status"))
        self.state["reason_code"], self.state["http_status"] = reason, status
        await asyncio.to_thread(self.store.interrupt_running)
        self.client.failover = self
        self.task = asyncio.create_task(self._run(), name="model-failover")

    def observe(self, values):
        if self.closed or values.get("model") != self.config.model:
            return
        now = utc_now()
        self.revision += 1
        if values.get("outcome") == "success":
            self.state.update(failure_since=None, last_success_at=now, reason_code="", reason="", http_status=None, next_attempt_at=None)
        elif values.get("outcome") == "error":
            reason = safe_error(values.get("error", ""), self.client.api_key)
            kind, status = classify(reason, values.get("http_status"))
            self.state.update(failure_since=self.state.get("failure_since") or now,
                last_failure_at=now, reason_code=kind, reason=reason, http_status=status)
        self.dirty = True

    def deferred(self):
        # Requests are retained while the replacement round/cooldown is active.
        since = self.state.get("failure_since")
        if not since:
            return False
        if self.probing:
            return True
        retry = self.state.get("next_attempt_at")
        return bool(retry and utc_now() < retry)

    def status(self):
        since = self.state.get("failure_since")
        return {"enabled": self.config.enabled, "current_model": self.config.model,
                "status": "probing" if self.probing else "degraded" if since else "healthy",
                "failure_since": since.isoformat() if since else None,
                "failure_duration_seconds": max(0, int((utc_now() - since).total_seconds())) if since else 0,
                "switch_after_seconds": OUTAGE_SECONDS,
                "next_attempt_at": self.state.get("next_attempt_at").isoformat() if self.state.get("next_attempt_at") else None,
                "reason": self.state.get("reason", "")}

    def mature_outage(self):
        since = self.state.get("failure_since")
        return bool(since and (utc_now() - since).total_seconds() >= OUTAGE_SECONDS)

    async def _new_probe(self, model):
        clone = VisionModelClient(self.config.base_url, self.client.api_key, model)
        await clone.client.aclose()
        clone.client = httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False)
        try:
            return await asyncio.wait_for(validate_model(clone), 90)
        finally:
            await clone.close()

    async def _run(self):
        # Startup validation is isolated from business logs and never switches
        # without a persisted 30-minute continuous outage.
        try:
            revision = self.revision
            try:
                await self._new_probe(self.config.model)
                if revision == self.revision:
                    self.observe({"model": self.config.model, "outcome": "success"})
            except Exception as exc:
                if revision == self.revision:
                    self.observe({"model": self.config.model, "outcome": "error", "error": str(exc)})
            while not self.closed:
                try:
                    if self.dirty:
                        self.dirty = False
                        await asyncio.to_thread(self.store.save_state, self.config, dict(self.state))
                    since = self.state.get("failure_since")
                    retry = self.state.get("next_attempt_at")
                    if (self.config.enabled and since and (utc_now() - since).total_seconds() >= OUTAGE_SECONDS
                            and (not retry or utc_now() >= retry)):
                        await self._replace()
                except Exception:
                    self.dirty = True
                    logger.exception("Model failover coordinator iteration failed")
                    await asyncio.sleep(5)
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise

    async def _replace(self):
        if self._round_lock.locked():
            return
        async with self._round_lock:
            since = self.state.get("failure_since")
            if not self.config.enabled or not since or (utc_now() - since).total_seconds() < OUTAGE_SECONDS:
                return
            await self._replace_owned()

    async def _replace_owned(self):
        config = self.config
        if await asyncio.to_thread(self.store.current) != config:
            return
        revision = self.revision
        self.probing = True
        incident = None
        attempts, candidates = [], []
        deadline = time.monotonic() + 180
        try:
            incident = await asyncio.to_thread(self.store.begin, config, self.state)
            # Recheck the original model before replacing it, particularly after
            # cooldown. A recovered model ends the continuous outage.
            try:
                await asyncio.wait_for(self._new_probe(config.model), min(90, deadline - time.monotonic()))
            except Exception as exc:
                self.observe({"model": config.model, "outcome": "error", "error": str(exc)})
            else:
                self.observe({"model": config.model, "outcome": "success"})
                await asyncio.to_thread(self.store.finish, incident, config, "skipped", "原模型已通过实际图片验证，取消替换并重置连续故障计时")
                return
            if self.state["reason_code"] in {"credentials_or_quota", "rate_limited"}:
                await asyncio.to_thread(self.store.finish, incident, config, "failure", "鉴权、余额或账户限流不能通过更换模型解决；未替换")
                return
            failed = json.loads(self.state.get("failed_candidates_json") or "{}")
            async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as discovery:
                response = await discovery.get(config.base_url + "/models", headers={"Authorization": "Bearer " + self.client.api_key})
                response.raise_for_status()
                candidates = rank_candidates(response.json().get("data", []), config.model)
            await asyncio.to_thread(self.store.progress, incident, candidates, attempts)
            attempted = 0
            for candidate in candidates:
                if failed.get(candidate, 0) > time.time():
                    continue
                remaining = deadline - time.monotonic()
                if attempted >= 5 or remaining <= 0:
                    break
                if not self.mature_outage():
                    await asyncio.to_thread(self.store.finish, incident, config, "skipped", "原模型业务调用已恢复，取消替换")
                    return
                attempted += 1
                started = time.monotonic()
                try:
                    result = await asyncio.wait_for(self._new_probe(candidate), remaining)
                    attempts.append({"model": candidate, "outcome": "success", **result})
                    await asyncio.to_thread(self.store.progress, incident, candidates, attempts)
                    if not self.mature_outage():
                        await asyncio.to_thread(self.store.finish, incident, config, "skipped", "原模型业务调用已恢复，取消替换")
                        return
                    target = await asyncio.to_thread(self.store.finish, incident, config, "success", "图片识别及两种业务 JSON 验证通过，自动替换", candidate)
                    if target:
                        # Keep in-flight HTTP clients open. Each request already
                        # captured its original model and immutable image.
                        self.client.economy_model = target.model
                        self.config = target
                        self.state = dict(failure_since=None, last_success_at=None, last_failure_at=None,
                                          reason_code="", reason="", http_status=None,
                                          next_attempt_at=None, failed_candidates_json="{}")
                        self.dirty = True
                    return
                except Exception as exc:
                    error = safe_error(f"{type(exc).__name__}: {exc}", self.client.api_key)
                    attempts.append({"model": candidate, "outcome": "failure", "error": error,
                                     "latency_ms": int((time.monotonic() - started) * 1000)})
                    failed[candidate] = time.time() + 1800
                    self.state["failed_candidates_json"] = json.dumps(failed)
                    await asyncio.to_thread(self.store.progress, incident, candidates, attempts)
            await asyncio.to_thread(self.store.finish, incident, config, "failure", "本轮没有通过验证的候选模型，保持原配置")
        except asyncio.CancelledError:
            if incident:
                await asyncio.to_thread(self.store.finish, incident, config, "skipped", "配置重载或服务关闭，中断探测；未替换")
            raise
        except Exception as exc:
            if incident:
                await asyncio.to_thread(self.store.finish, incident, config, "failure", "发现或探测失败：" + safe_error(exc, self.client.api_key))
            else:
                raise
        finally:
            self.probing = False
            if self.state.get("failure_since"):
                self.state["next_attempt_at"] = utc_now() + timedelta(minutes=5)
                self.dirty = True

    async def close(self):
        self.closed = True
        self.client.failover = None
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.state:
            await asyncio.to_thread(self.store.save_state, self.config, dict(self.state))
