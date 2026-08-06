import cv2
import numpy as np

from backend.detectors.black_screen import is_black_screen


def jpeg(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return encoded.tobytes()


def test_black_frame_detected():
    black, metrics = is_black_screen(jpeg(np.zeros((100, 100, 3), dtype=np.uint8)), 18, 12, 0.92)
    assert black
    assert metrics["near_black_ratio"] == 1.0


def test_bright_frame_not_black():
    image = np.full((100, 100, 3), 220, dtype=np.uint8)
    black, _ = is_black_screen(jpeg(image), 18, 12, 0.92)
    assert not black

