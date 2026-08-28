from __future__ import annotations

import logging
import math

import cv2
import numpy as np

from backend.schemas import Detection


logger = logging.getLogger(__name__)

DEFAULT_COLOR = (200, 200, 200)
HIGHLIGHT_COLOR = (40, 40, 245)
ZONE_FILL = (45, 45, 210)
ZONE_EDGE = (45, 45, 235)
CLASS_COLORS: dict[str, tuple[int, int, int]] = {
    "person": (117, 229, 71),
    "cell phone": (230, 180, 50),
    "mobile phone": (230, 180, 50),
    "fire": (35, 35, 245),
    "smoke": (30, 150, 255),
}


def detection_label(detection: Detection) -> str:
    return f"{detection.class_name} {detection.confidence * 100:.1f}%"


def annotate_detections(
    jpeg: bytes,
    detections: list[Detection],
    zone: list[tuple[float, float]] | None = None,
    highlighted_track_ids: set[int] | None = None,
    show_track_ids: bool = False,
) -> bytes:
    if not detections and not zone:
        return jpeg
    try:
        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return jpeg
        height, width = image.shape[:2]
        base = min(width, height)
        thickness = max(2, round(base / 320))
        font_scale = max(0.45, base / 1000)

        if zone and len(zone) >= 3:
            polygon = np.asarray(
                [[round(x * width), round(y * height)] for x, y in zone], dtype=np.int32
            )
            layer = image.copy()
            cv2.fillPoly(layer, [polygon], ZONE_FILL)
            cv2.addWeighted(layer, 0.20, image, 0.80, 0, image)
            cv2.polylines(image, [polygon], True, ZONE_EDGE, thickness, cv2.LINE_AA)

        for detection in detections:
            box = _clamp_box(detection.box, width, height)
            if box is None:
                logger.debug(
                    "Skipping invalid detection box for %s: %s", detection.class_name, detection.box
                )
                continue
            left, top, right, bottom = box
            highlighted = bool(highlighted_track_ids and detection.track_id in highlighted_track_ids)
            color = HIGHLIGHT_COLOR if highlighted else CLASS_COLORS.get(detection.class_name, DEFAULT_COLOR)
            cv2.rectangle(
                image, (left, top), (right, bottom), color, thickness + 1 if highlighted else thickness, cv2.LINE_AA
            )
            label = detection_label(detection)
            if show_track_ids and detection.track_id is not None:
                label = f"#{detection.track_id} {label}"
            _draw_label(image, label, left, top, color, font_scale, thickness)

        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])
        return encoded.tobytes() if ok else jpeg
    except Exception:
        logger.exception("Failed to annotate evidence image")
        return jpeg


def _clamp_box(
    box: tuple[float, float, float, float], width: int, height: int
) -> tuple[int, int, int, int] | None:
    x1, y1, x2, y2 = box
    if any(math.isnan(value) or math.isinf(value) for value in (x1, y1, x2, y2)):
        return None
    left = max(0, min(width - 1, round(x1 * width)))
    top = max(0, min(height - 1, round(y1 * height)))
    right = max(0, min(width - 1, round(x2 * width)))
    bottom = max(0, min(height - 1, round(y2 * height)))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _draw_label(
    image: np.ndarray,
    label: str,
    left: int,
    top: int,
    color: tuple[int, int, int],
    font_scale: float,
    thickness: int,
) -> None:
    (text_width, text_height), baseline = cv2.getTextSize(
        label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, max(1, thickness)
    )
    height, width = image.shape[:2]
    pad = 4
    box_width = text_width + 2 * pad
    box_height = text_height + baseline + 2 * pad
    origin_x = max(0, min(left, width - box_width))
    origin_y = top - box_height
    if origin_y < 0:
        origin_y = min(top + pad, height - box_height)
    origin_y = max(0, min(origin_y, height - box_height))
    cv2.rectangle(
        image, (origin_x, origin_y), (origin_x + box_width, origin_y + box_height), color, -1
    )
    brightness = 0.299 * color[2] + 0.587 * color[1] + 0.114 * color[0]
    text_color = (12, 12, 12) if brightness > 140 else (255, 255, 255)
    cv2.putText(
        image,
        label,
        (origin_x + pad, origin_y + pad + text_height),
        cv2.FONT_HERSHEY_SIMPLEX,
        font_scale,
        text_color,
        thickness,
        cv2.LINE_AA,
    )
