"""Synthetic-image capability checks; never sends a monitoring image."""
from __future__ import annotations

import base64
import secrets
import time

import cv2
import numpy as np

from backend.schemas import Mode
from backend.vlm import VLMError, extract_json


def synthetic_image():
    image = np.full((280, 480, 3), 255, dtype=np.uint8)
    colors = {"red": (0, 0, 240), "green": (0, 180, 0), "blue": (240, 0, 0), "yellow": (0, 220, 240)}
    circle_color = secrets.choice(list(colors))
    count = secrets.choice([1, 2, 3, 4])
    cv2.circle(image, (240, 60), 36, colors[circle_color], -1)
    for i in range(count):
        cv2.rectangle(image, (25 + i * 115, 150), (95 + i * 115, 220), colors["red"], -1)
    ok, encoded = cv2.imencode(".jpg", image)
    if not ok:
        raise RuntimeError("Cannot generate model verification image")
    return encoded.tobytes(), {"red_squares": count, "circle_color": circle_color}


async def validate_model(client):
    started = time.monotonic()
    jpeg, expected = synthetic_image()
    body = {
        "model": client.economy_model, "temperature": 0, "max_completion_tokens": 150,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": 'Inspect the attached image. Count red SQUARES (exclude circles) and identify the circle color. Return only JSON: {"red_squares":integer,"circle_color":"red|green|blue|yellow"}.'},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}},
        ]}],
    }
    response = await client.client.post(client.base_url + "/chat/completions",
        headers={"Authorization": "Bearer " + client.api_key}, json=body, timeout=30)
    if response.is_error:
        raise VLMError(f"图片探测 HTTP {response.status_code}: {response.text[:500]}")
    try:
        content = response.json()["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        actual = extract_json(content)
        if actual != expected or type(actual.get("red_squares")) is not int:
            raise ValueError("图片识别结果不匹配")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise VLMError("图片识别验证失败，不能确认模型具有视觉能力") from exc
    # Both production response contracts must work with the same image and
    # production parameters. These calls have no business log writer.
    await client.analyze_behaviors({Mode.PHONE_USE, Mode.SMOKING, Mode.OFF_DUTY}, jpeg)
    await client.analyze_off_duty(jpeg)
    return {"ok": True, "model": client.economy_model,
            "checks": ["image_recognition", "behavior_json", "off_duty_json"],
            "latency_ms": int((time.monotonic() - started) * 1000)}
