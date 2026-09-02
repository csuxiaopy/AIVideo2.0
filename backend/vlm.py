from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from backend.schemas import BehaviorVLMResult, Mode, VLMResult


SYSTEM_PROMPT = """你是监控视频行为检测器，只判断请求中指定的行为。每次输入一张当前监控图片。
不得根据身份、服装或画面外信息推断。画面模糊、遮挡或证据不足必须返回 uncertain。
status 只能是 confirmed、suspected、uncertain、none。只输出 JSON 对象，格式为：
{"results":[{"mode":"请求的模式","status":"...","confidence":0到1,"evidence_frames":[0],"reason":"...","need_review":false}]}。
results 必须且只能包含请求中列出的每个模式一次，不能缺少、重复或增加模式。
phone_use 只有明确看到人员正在操作或注视手机才可 confirmed；仅看到手机不能确认。
smoking 只有明确看到持烟、吸食动作或可关联的烟雾证据才可 confirmed。
不要把喝水、吃东西、摸脸、打电话或普通手部动作误判为抽烟。"""


@dataclass
class VLMResponse:
    results: dict[Mode, VLMResult]
    request_id: str | None
    usage: dict[str, Any]
    latency_ms: int
    provider: str
    model: str


class VLMError(RuntimeError):
    def __init__(self, message: str, request_id: str | None = None):
        super().__init__(message)
        self.request_id = request_id


def extract_json(text: str) -> dict[str, Any]:
    value = text.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        start, end = value.find("{"), value.rfind("}")
        if start >= 0 and end > start:
            return json.loads(value[start:end + 1])
        raise


class VisionModelClient:
    def __init__(self, base_url: str, api_key: str, economy_model: str, enhanced_model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.economy_model = economy_model
        self.enhanced_model = enhanced_model
        self.client = httpx.AsyncClient(timeout=90)

    async def close(self) -> None:
        await self.client.aclose()

    async def analyze_behaviors(
        self, modes: set[Mode], frame: bytes, enhanced: bool = False
    ) -> VLMResponse:
        allowed = {Mode.PHONE_USE, Mode.SMOKING}
        if not modes or not modes <= allowed:
            raise ValueError("联合行为检测模式必须是玩手机或吸烟")
        model = self.enhanced_model if enhanced else self.economy_model
        if not self.base_url or not self.api_key:
            raise VLMError("视觉大模型尚未配置")
        content: list[dict[str, Any]] = [{
            "type": "text",
            "text": "检测模式：" + "、".join(sorted(mode.value for mode in modes)) + "。请检查当前单帧并严格返回 JSON。",
        }]
        encoded = base64.b64encode(frame).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "temperature": 0,
            "max_completion_tokens": 500,
            "response_format": {"type": "json_object"},
        }
        started = time.perf_counter()
        response = await self.client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=body,
        )
        if response.status_code == 429:
            retry_after = min(30.0, float(response.headers.get("Retry-After", "1") or 1))
            import asyncio

            await asyncio.sleep(retry_after)
            response = await self.client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=body,
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
        if response.is_error:
            code = ""
            message = response.text[:500]
            try:
                payload = response.json()
                error = payload.get("error", payload)
                if isinstance(error, dict):
                    code = str(error.get("code", ""))
                    message = str(error.get("message", message))[:500]
            except ValueError:
                pass
            raise VLMError(
                f"大模型 HTTP {response.status_code}" + (f" {code}" if code else "") + f"：{message}",
                request_id,
            )
        payload = response.json()
        request_id = payload.get("id") or request_id
        message = payload["choices"][0]["message"]["content"]
        if isinstance(message, list):
            message = "".join(str(item.get("text", "")) for item in message if isinstance(item, dict))
        try:
            combined = BehaviorVLMResult.model_validate(extract_json(str(message)))
        except Exception as exc:
            raise VLMError(f"大模型返回格式错误：{str(exc)[:300]}", request_id) from exc
        results = {item.mode: item for item in combined.results}
        if set(results) != modes:
            raise VLMError("大模型返回的检测模式与请求不一致", request_id)
        return VLMResponse(
            results=results,
            request_id=request_id,
            usage=payload.get("usage", {}),
            latency_ms=latency_ms,
            provider="openai_compatible",
            model=model,
        )

    async def tiered_analyze_behaviors(self, modes: set[Mode], frame: bytes) -> VLMResponse:
        economy = await self.analyze_behaviors(modes, frame, enhanced=False)
        if all(result.status == "none" for result in economy.results.values()):
            return economy
        return await self.analyze_behaviors(modes, frame, enhanced=True)

    async def test(self) -> dict[str, Any]:
        started = time.perf_counter()
        response = await self.client.get(
            f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"}
        )
        if response.is_error:
            raise VLMError(f"模型连通性测试失败：HTTP {response.status_code}")
        return {"ok": True, "latency_ms": int((time.perf_counter() - started) * 1000)}

