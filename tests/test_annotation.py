import cv2
import numpy as np

from backend.annotation import annotate_detections, detection_label
from backend.schemas import Detection


def make_jpeg(width: int = 320, height: int = 240) -> bytes:
    image = np.full((height, width, 3), 24, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return encoded.tobytes()


def decode(jpeg: bytes) -> np.ndarray:
    rendered = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert rendered is not None
    return rendered


def test_detection_label_formats_class_and_confidence():
    detection = Detection(class_id=0, class_name="person", confidence=0.924, box=(0, 0, 0, 0))
    assert detection_label(detection) == "person 92.4%"
    detection = Detection(class_id=73, class_name="cell phone", confidence=0.728, box=(0, 0, 0, 0))
    assert detection_label(detection) == "cell phone 72.8%"


def test_annotate_single_person_draws_box_and_label():
    jpeg = make_jpeg()
    annotated = annotate_detections(
        jpeg,
        [Detection(class_id=0, class_name="person", confidence=0.924, box=(0.25, 0.25, 0.75, 0.75))],
    )
    assert annotated != jpeg
    rendered = decode(annotated)
    edge = rendered[70:170, 78:84]
    assert int(edge[:, :, 1].max()) > 180
    label = rendered[36:56, 82:110]
    assert int(label[:, :, 1].mean()) > 150


def test_annotate_multiple_persons_each_drawn():
    jpeg = make_jpeg()
    annotated = annotate_detections(
        jpeg,
        [
            Detection(class_id=0, class_name="person", confidence=0.94, box=(0.05, 0.1, 0.25, 0.9)),
            Detection(class_id=0, class_name="person", confidence=0.76, box=(0.75, 0.1, 0.95, 0.9)),
        ],
    )
    rendered = decode(annotated)
    left_person = rendered[60:180, 14:20]
    right_person = rendered[60:180, 302:308]
    assert int(left_person[:, :, 1].max()) > 180
    assert int(right_person[:, :, 1].max()) > 180


def test_annotate_person_and_phone_use_distinct_colors():
    jpeg = make_jpeg()
    annotated = annotate_detections(
        jpeg,
        [
            Detection(class_id=0, class_name="person", confidence=0.941, box=(0.1, 0.2, 0.45, 0.9)),
            Detection(class_id=73, class_name="cell phone", confidence=0.728, box=(0.6, 0.2, 0.85, 0.5)),
        ],
    )
    rendered = decode(annotated)
    person_edge = rendered[90:190, 30:36]
    phone_edge = rendered[90:190, 190:196]
    assert int(person_edge[:, :, 1].max()) > 180
    assert int(phone_edge[:, :, 0].max()) > 180
    assert int(phone_edge[:, :, 2].max()) < int(person_edge[:, :, 1].max())


def test_annotate_fire_and_smoke_drawn_together():
    jpeg = make_jpeg()
    annotated = annotate_detections(
        jpeg,
        [
            Detection(class_id=0, class_name="fire", confidence=0.861, box=(0.1, 0.1, 0.3, 0.5)),
            Detection(class_id=1, class_name="smoke", confidence=0.714, box=(0.6, 0.1, 0.9, 0.5)),
        ],
    )
    rendered = decode(annotated)
    fire_edge = rendered[30:110, 30:38]
    smoke_edge = rendered[30:110, 190:198]
    assert int(fire_edge[:, :, 2].max()) > 200
    assert int(smoke_edge[:, :, 1].max()) > int(fire_edge[:, :, 1].mean())
    assert int(smoke_edge[:, :, 2].max()) > 200


def test_annotate_skips_detections_below_business_threshold():
    jpeg = make_jpeg()
    all_detections = [
        Detection(class_id=0, class_name="person", confidence=0.91, box=(0.05, 0.1, 0.25, 0.9)),
        Detection(class_id=0, class_name="person", confidence=0.20, box=(0.75, 0.1, 0.95, 0.9)),
    ]
    qualified = [item for item in all_detections if item.confidence >= 0.45]
    annotated = annotate_detections(jpeg, qualified)
    rendered = decode(annotated)
    assert int(rendered[60:180, 14:20][:, :, 1].max()) > 180
    assert int(rendered[60:180, 302:308][:, :, 1].max()) < 100


def test_annotate_invalid_boxes_are_skipped_without_crash():
    jpeg = make_jpeg()
    annotated = annotate_detections(
        jpeg,
        [
            Detection(class_id=0, class_name="person", confidence=0.9, box=(float("nan"), 0.1, 0.4, 0.6)),
            Detection(class_id=0, class_name="person", confidence=0.9, box=(float("inf"), 0.1, 0.4, 0.6)),
            Detection(class_id=0, class_name="person", confidence=0.9, box=(0.5, 0.5, 0.2, 0.2)),
            Detection(class_id=0, class_name="person", confidence=0.9, box=(0.5, 0.5, 0.5, 0.5)),
            Detection(class_id=0, class_name="person", confidence=0.9, box=(-2.0, -2.0, 5.0, 5.0)),
        ],
    )
    rendered = decode(annotated)
    assert rendered.shape == (240, 320, 3)


def test_annotate_zone_and_highlighted_intruder():
    jpeg = make_jpeg()
    annotated = annotate_detections(
        jpeg,
        [
            Detection(class_id=0, class_name="person", confidence=0.925, box=(0.4, 0.65, 0.6, 0.85), track_id=3),
            Detection(class_id=0, class_name="person", confidence=0.61, box=(0.05, 0.05, 0.2, 0.2), track_id=9),
        ],
        zone=[(0.1, 0.6), (0.9, 0.6), (0.9, 0.9), (0.1, 0.9)],
        highlighted_track_ids={3},
    )
    rendered = decode(annotated)
    zone_edge = rendered[140:148, 100:220]
    assert int(zone_edge[:, :, 2].max()) > 200
    intruder_edge = rendered[175:200, 126:132]
    assert int(intruder_edge[:, :, 2].max()) > 200
    other_edge = rendered[40:60, 14:20]
    assert int(other_edge[:, :, 1].max()) > 180


def test_annotate_off_duty_without_people_keeps_zone_only():
    jpeg = make_jpeg()
    annotated = annotate_detections(jpeg, [], zone=[(0.2, 0.3), (0.8, 0.3), (0.8, 0.8), (0.2, 0.8)])
    rendered = decode(annotated)
    zone_edge = rendered[70:78, 120:220]
    assert int(zone_edge[:, :, 2].max()) > 200
    assert int(rendered[:, :, 1].mean()) < 100


def test_annotate_empty_input_returns_original_bytes():
    jpeg = make_jpeg()
    assert annotate_detections(jpeg, []) == jpeg


def test_annotate_label_near_top_stays_inside_image():
    jpeg = make_jpeg()
    annotated = annotate_detections(
        jpeg,
        [Detection(class_id=0, class_name="person", confidence=0.88, box=(0.3, 0.0, 0.6, 0.4))],
    )
    rendered = decode(annotated)
    assert rendered.shape == (240, 320, 3)
    label = rendered[4:28, 96:130]
    assert int(label[:, :, 1].max()) > 180
