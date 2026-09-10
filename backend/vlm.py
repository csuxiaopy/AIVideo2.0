from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from backend.schemas import BehaviorVLMResult, Mode, VLMResult
from backend.repository import as_json


SYSTEM_PROMPT = """你是监控视频行为检测器，只判断请求中指定的行为。每次输入一张当前监控图片。
不得根据身份、服装或画面外信息推断。画面模糊、遮挡或证据不足必须返回 uncertain。
status 只能是 confirmed、suspected、uncertain、none。只输出 JSON 对象，格式为：
{"results":[{"mode":"请求的模式","status":"...","confidence":0到1,"evidence_frames":[0],"reason":"...","need_review":false}]}。
results 必须且只能包含请求中列出的每个模式一次，不能缺少、重复或增加模式。
phone_use 只判断图片中工作人员是否正在进行非工作性的玩手机行为。工作人员应根据其位于柜台、工位或岗位区域中的空间位置判断，不得根据服装或身份特征猜测。
phone_use 只有清晰看到工作人员正在持续注视、滑动、点击手机，或进行其他明显的非工作性手机操作，并且不存在下述排除场景时，才可返回 confirmed；仅看到或拿着手机不能确认。
phone_use 出现以下任一场景必须返回 none：工作人员前方有客户且正在接待或服务客户；工作人员一只手正在操作鼠标、另一只手拿手机；有明确工作场景证据表明手机用于扫码、登记、拍照、核对信息、联系客户或处理业务。
phone_use 无法确认人物是否为工作人员、物体是否为手机，或无法区分工作用途和娱乐用途时，必须返回 uncertain，不得返回 confirmed。
smoking 只有明确看到持烟、吸食动作或可关联的烟雾证据才可 confirmed。
不要把喝水、吃东西、摸脸、打电话或普通手部动作误判为抽烟。"""
logger = logging.getLogger(__name__)


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
    def __init__(self, base_url: str, api_key: str, economy_model: str, log_writer=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.economy_model = economy_model
        self.log_writer = log_writer
        self.client = httpx.AsyncClient(timeout=90)

    def _log_call(self, **values: Any) -> None:
        writer = getattr(self, "log_writer", None)
        if not writer:
            return
        try:
            writer(**values)
        except Exception:
            logger.exception("Failed to persist model call log")

    async def close(self) -> None:
        await self.client.aclose()

    async def analyze_behaviors(
        self, modes: set[Mode], frame: bytes,
        camera_id: str | None = None, camera_name: str = "",
    ) -> VLMResponse:
        allowed = {Mode.PHONE_USE, Mode.SMOKING}
        if not modes or not modes <= allowed:
            raise ValueError("联合行为检测模式必须是玩手机或吸烟")
        model = self.economy_model
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
        stage = "single"
        common = {
            "camera_id": camera_id, "camera_name": camera_name,
            "modes_json": as_json(sorted(mode.value for mode in modes)),
            "stage": stage, "provider": "openai_compatible", "model": model,
        }
        try:
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
        except httpx.HTTPError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            error_text = f"{type(exc).__name__}: {str(exc)[:1000]}"
            self._log_call(**common, request_id=None, http_status=None, outcome="error",
                           latency_ms=latency_ms, usage_json="{}", raw_response="",
                           parsed_response_json="{}", error=error_text)
            raise VLMError(f"大模型请求失败：{str(exc)[:300]}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
        raw_response = response.text
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
            error_text = f"大模型 HTTP {response.status_code}" + (f" {code}" if code else "") + f"：{message}"
            self._log_call(**common, request_id=request_id, http_status=response.status_code,
                           outcome="error", latency_ms=latency_ms, usage_json="{}",
                           raw_response=raw_response, parsed_response_json="{}", error=error_text[:2000])
            raise VLMError(error_text, request_id)
        try:
            payload = response.json()
        except ValueError as exc:
            error_text = f"大模型返回格式错误：{str(exc)[:300]}"
            self._log_call(**common, request_id=request_id, http_status=response.status_code,
                           outcome="error", latency_ms=latency_ms, usage_json="{}",
                           raw_response=raw_response, parsed_response_json="{}", error=error_text)
            raise VLMError(error_text, request_id) from exc
        request_id = payload.get("id") or request_id
        try:
            message = payload["choices"][0]["message"]["content"]
            if isinstance(message, list):
                message = "".join(str(item.get("text", "")) for item in message if isinstance(item, dict))
            combined = BehaviorVLMResult.model_validate(extract_json(str(message)))
        except Exception as exc:
            error_text = f"大模型返回格式错误：{str(exc)[:300]}"
            self._log_call(**common, request_id=request_id, http_status=response.status_code,
                           outcome="error", latency_ms=latency_ms,
                           usage_json=as_json(payload.get("usage", {})), raw_response=raw_response,
                           parsed_response_json="{}", error=error_text)
            raise VLMError(error_text, request_id) from exc
        results = {item.mode: item for item in combined.results}
        if set(results) != modes:
            error_text = "大模型返回的检测模式与请求不一致"
            self._log_call(**common, request_id=request_id, http_status=response.status_code,
                           outcome="error", latency_ms=latency_ms,
                           usage_json=as_json(payload.get("usage", {})), raw_response=raw_response,
                           parsed_response_json=as_json(combined.model_dump(mode="json")), error=error_text)
            raise VLMError(error_text, request_id)
        self._log_call(**common, request_id=request_id, http_status=response.status_code,
                       outcome="success", latency_ms=latency_ms,
                       usage_json=as_json(payload.get("usage", {})), raw_response=raw_response,
                       parsed_response_json=as_json(combined.model_dump(mode="json")), error=None)
        return VLMResponse(
            results=results,
            request_id=request_id,
            usage=payload.get("usage", {}),
            latency_ms=latency_ms,
            provider="openai_compatible",
            model=model,
        )

    async def test(self) -> dict[str, Any]:
        started = time.perf_counter()
        response = await self.client.get(
            f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"}
        )
        if response.is_error:
            raise VLMError(f"模型连通性测试失败：HTTP {response.status_code}")
        return {"ok": True, "latency_ms": int((time.perf_counter() - started) * 1000)}

