import cv2
import numpy as np

from backend.media import (
    ObjectOverlay,
    PersonOverlay,
    SafetyOverlay,
    draw_object_overlays,
    draw_person_overlays,
    draw_safety_overlays,
    phone_overlays,
)
from backend.schemas import Detection


def test_person_overlay_draws_box_without_changing_empty_preview():
    image = np.full((240, 320, 3), 24, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    jpeg = encoded.tobytes()

    assert draw_person_overlays(jpeg, []) == jpeg

    annotated = draw_person_overlays(
        jpeg,
        [PersonOverlay(box=(0.25, 0.2, 0.75, 0.9), confidence=0.91, track_id=7)],
    )
    rendered = cv2.imdecode(np.frombuffer(annotated, dtype=np.uint8), cv2.IMREAD_COLOR)

    assert annotated != jpeg
    assert rendered is not None
    # The left edge of the normalized person box should contain the green overlay.
    edge = rendered[45:220, 78:84]
    assert int(edge[:, :, 1].max()) > 180
    assert int(edge[:, :, 1].mean()) > int(edge[:, :, 0].mean())


def test_safety_overlay_draws_intrusion_fire_and_smoke_colors():
    image = np.full((240, 320, 3), 24, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    annotated = draw_safety_overlays(
        encoded.tobytes(),
        [
            SafetyOverlay("fire", (0.1, 0.1, 0.3, 0.5), 0.91),
            SafetyOverlay("smoke", (0.6, 0.1, 0.9, 0.5), 0.78),
        ],
        intrusion_zone=[(0.1, 0.6), (0.9, 0.6), (0.9, 0.9), (0.1, 0.9)],
    )
    rendered = cv2.imdecode(np.frombuffer(annotated, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert rendered is not None
    # OpenCV uses BGR: fire is red; smoke carries more green than fire.
    fire_edge = rendered[24:120, 30:38]
    smoke_edge = rendered[24:120, 190:198]
    assert int(fire_edge[:, :, 2].max()) > 200
    assert int(smoke_edge[:, :, 1].max()) > int(fire_edge[:, :, 1].mean())


def test_phone_overlays_accept_both_phone_names_and_ignore_other_classes():
    detections = [
        Detection(class_id=73, class_name="cell phone", confidence=0.72, box=(0.1, 0.1, 0.2, 0.3)),
        Detection(class_id=73, class_name="mobile phone", confidence=0.81, box=(0.4, 0.1, 0.5, 0.3)),
        Detection(class_id=0, class_name="person", confidence=0.94, box=(0.0, 0.0, 0.8, 0.9)),
    ]
    overlays = phone_overlays(detections)
    assert [item.kind for item in overlays] == ["phone", "phone"]
    assert [item.confidence for item in overlays] == [0.72, 0.81]


def test_phone_overlay_draws_distinct_box_and_keeps_empty_preview():
    image = np.full((240, 320, 3), 24, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    jpeg = encoded.tobytes()
    assert draw_object_overlays(jpeg, []) == jpeg

    annotated = draw_object_overlays(
        jpeg, [ObjectOverlay("phone", (0.25, 0.2, 0.75, 0.8), 0.72)]
    )
    rendered = cv2.imdecode(np.frombuffer(annotated, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert rendered is not None
    edge = rendered[48:190, 78:84]
    assert int(edge[:, :, 1].max()) > 170
    assert int(edge[:, :, 2].max()) > 200
