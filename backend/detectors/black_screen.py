from __future__ import annotations

import cv2
import numpy as np


def decode_jpeg(jpeg: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("JPEG 解码失败")
    return image


def black_metrics(jpeg: bytes) -> dict[str, float]:
    image = decode_jpeg(jpeg)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    histogram = cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel()
    probabilities = histogram / max(1.0, histogram.sum())
    probabilities = probabilities[probabilities > 0]
    entropy = float(-(probabilities * np.log2(probabilities)).sum())
    return {
        "mean": float(gray.mean()),
        "std": float(gray.std()),
        "near_black_ratio": float((gray < 25).mean()),
        "entropy": entropy,
    }


def is_black_screen(jpeg: bytes, mean_max: float, std_max: float, ratio_min: float) -> tuple[bool, dict[str, float]]:
    metrics = black_metrics(jpeg)
    black = (
        metrics["mean"] <= mean_max
        and metrics["std"] <= std_max
        and metrics["near_black_ratio"] >= ratio_min
    )
    return black, metrics

