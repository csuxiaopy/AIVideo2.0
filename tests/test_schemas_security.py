import pytest

from backend.schemas import (
    CameraBatchCreate,
    CameraCreate,
    CameraPatch,
    DisplaySettingsUpdate,
    GeometrySpec,
    Mode,
    VLMResult,
)
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


def test_camera_patch_supports_complete_business_edit():
    patch = CameraPatch(
        id="hall-renamed",
        name="新名称",
        enabled=False,
        scene_type="security_area",
        modes=["black_screen", "fire_smoke", "fire_smoke"],
        geometry={"post_roi": [], "flow_line": [], "intrusion_zone": None},
        schedule={"timezone": "Asia/Shanghai", "weekly": {}, "holidays": []},
        frame_interval_seconds=20,
    )
    assert patch.id == "hall-renamed"
    assert [mode.value for mode in patch.modes or []] == ["black_screen", "fire_smoke"]
    assert patch.frame_interval_seconds == 20


def test_display_settings_accept_partial_updates():
    assert DisplaySettingsUpdate(show_traffic_report=False).model_dump(exclude_none=True) == {
        "show_traffic_report": False
    }


def test_camera_batch_limit_and_source_schemes():
    item = {"id": "batch-1", "name": "批量摄像头", "rtsp_url": "rtmp://user:p@host/live"}
    assert len(CameraBatchCreate(items=[item]).items) == 1
    with pytest.raises(ValueError):
        CameraBatchCreate(items=[item] * 501)

    camera = CameraCreate(
        id="http-stream",
        name="HTTP 视频源",
        rtsp_url="https://example.invalid/live.m3u8",
        modes=[Mode.BLACK_SCREEN],
    )
    assert camera.rtsp_url.startswith("https://")
