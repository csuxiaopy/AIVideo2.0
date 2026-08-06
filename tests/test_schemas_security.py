import json

import pytest

from backend.schemas import CameraCreate, GeometrySpec, Mode, VLMResult
from backend.security import SecretCipher, redact_rtsp, sign_webhook
from backend.vlm import extract_json


def test_camera_requires_roi_and_line():
    with pytest.raises(ValueError):
        CameraCreate(id="c1", name="c1", rtsp_url="rtsp://host/live", modes=[Mode.OFF_DUTY])
    camera = CameraCreate(
        id="c1", name="c1", rtsp_url="rtsp://host/live",
        modes=[Mode.OFF_DUTY, Mode.PEOPLE_FLOW],
        geometry=GeometrySpec(post_roi=[(0,0),(1,0),(1,1)], flow_line=[(0.5,0),(0.5,1)]),
    )
    assert len(camera.modes) == 2


def test_cipher_redaction_and_signature():
    cipher = SecretCipher("test-secret")
    encrypted = cipher.encrypt("api-key")
    assert cipher.decrypt(encrypted) == "api-key"
    assert redact_rtsp("rtsp://admin:pass@10.0.0.1/live") == "rtsp://***:***@10.0.0.1/live"
    assert sign_webhook("secret", "1", {"a": 1}) == sign_webhook("secret", "1", {"a": 1})


def test_vlm_json_and_mode_validation():
    payload = extract_json('```json\n{"mode":"smoking","status":"confirmed","confidence":0.9}\n```')
    result = VLMResult.model_validate(payload)
    assert result.mode == Mode.SMOKING


def test_intrusion_requires_named_zone():
    with pytest.raises(ValueError):
        CameraCreate(
            id="security-1", name="security", rtsp_url="rtsp://camera/live",
            scene_type="security_area", modes=[Mode.INTRUSION],
        )
    camera = CameraCreate(
        id="security-1", name="security", rtsp_url="rtsp://camera/live",
        scene_type="security_area",
        modes=[Mode.INTRUSION, Mode.FIRE_SMOKE, Mode.BLACK_SCREEN],
        geometry={
            "intrusion_zone": {
                "name": "warehouse",
                "points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]],
            }
        },
    )
    assert camera.geometry.intrusion_zone.name == "warehouse"
