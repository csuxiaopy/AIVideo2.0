import pytest

from backend.schemas import (
    AlertBatchDelete,
    BehaviorVLMResult,
    CameraBatchCreate,
    CameraBatchDelete,
    CameraCreate,
    CameraPatch,
    DisplaySettingsUpdate,
    GeometrySpec,
    Mode,
    ModelSettingsUpdate,
    OffDutyVLMResult,
    VLMResult,
)
from backend.security import SecretCipher, redact_rtsp, sign_webhook
from backend.api.presenters import camera_public
from backend.vlm import extract_json


def test_camera_requires_post_roi_and_people_flow_defaults_to_full_screen_roi():
    with pytest.raises(ValueError):
        CameraCreate(id="c1", name="c1", rtsp_url="rtsp://host/live", modes=[Mode.OFF_DUTY])
    camera = CameraCreate(
        id="c1", name="c1", rtsp_url="rtsp://host/live",
        modes=[Mode.OFF_DUTY, Mode.PEOPLE_FLOW],
        geometry=GeometrySpec(post_roi=[(0,0),(1,0),(1,1)]),
    )
    assert len(camera.modes) == 2
    assert camera.geometry.flow_roi == [(0, 0), (1, 0), (1, 1), (0, 1)]


def test_alert_batch_delete_deduplicates_and_validates_ids():
    assert AlertBatchDelete(alert_ids=[3, 1, 3]).alert_ids == [3, 1]
    with pytest.raises(ValueError):
        AlertBatchDelete(alert_ids=[])
    with pytest.raises(ValueError):
        AlertBatchDelete(alert_ids=[0])
    with pytest.raises(ValueError):
        AlertBatchDelete(alert_ids=list(range(1, 502)))


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


def test_combined_vlm_result_rejects_duplicate_and_non_behavior_modes():
    duplicate = {
        "results": [
            {"mode": "smoking", "status": "none", "confidence": 0.9},
            {"mode": "smoking", "status": "confirmed", "confidence": 0.9},
        ]
    }
    with pytest.raises(ValueError):
        BehaviorVLMResult.model_validate(duplicate)
    with pytest.raises(ValueError):
        BehaviorVLMResult.model_validate({
            "results": [{"mode": "intrusion", "status": "none", "confidence": 0.9}]
        })


def test_combined_vlm_result_accepts_phone_smoking_and_off_duty():
    result = BehaviorVLMResult.model_validate({
        "results": [
            {"mode": "phone_use", "status": "none", "confidence": 0.9},
            {"mode": "smoking", "status": "none", "confidence": 0.9},
            {"mode": "off_duty", "status": "confirmed", "confidence": 0.98},
        ]
    })
    assert {item.mode for item in result.results} == {
        Mode.PHONE_USE, Mode.SMOKING, Mode.OFF_DUTY
    }


def test_off_duty_vlm_result_only_accepts_off_duty_mode():
    result = OffDutyVLMResult.model_validate({
        "result": {"mode": "off_duty", "status": "confirmed", "confidence": 0.98}
    })
    assert result.result.mode == Mode.OFF_DUTY
    with pytest.raises(ValueError):
        OffDutyVLMResult.model_validate({
            "result": {"mode": "smoking", "status": "none", "confidence": 0.9}
        })


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
        geometry={"post_roi": [], "intrusion_zone": None},
        schedule={"timezone": "Asia/Shanghai", "weekly": {}, "holidays": []},
        frame_interval_seconds=20,
    )
    assert patch.id == "hall-renamed"
    assert [mode.value for mode in patch.modes or []] == ["black_screen", "fire_smoke"]
    assert patch.frame_interval_seconds == 20


def test_camera_public_discards_historical_flow_geometry():
    from types import SimpleNamespace

    camera = SimpleNamespace(
        id="legacy-flow", name="旧入口", enabled=True, scene_type="customer_area",
        rtsp_url_encrypted=SecretCipher("test-secret").encrypt("rtsp://host/live"),
        online=True, last_seen_at=None, last_frame_at=None, last_analysis_at=None,
        frame_interval_seconds=1, last_error=None, modes_json='["people_flow"]',
        geometry_json='{"flow_line":[[0.1,0.5],[0.9,0.5]]}',
        schedule_json="{}", options_json="{}", created_at=None, updated_at=None,
    )
    result = camera_public(camera, SecretCipher("test-secret"))
    assert "flow_line" not in result["geometry"]
    assert "flow_zone" not in result["geometry"]
    assert result["geometry"]["flow_roi"] == [(0, 0), (1, 0), (1, 1), (0, 1)]


def test_phone_interval_cannot_exceed_judgment_duration():
    with pytest.raises(ValueError):
        CameraCreate(
            id="phone-1", name="phone", rtsp_url="rtsp://host/live", modes=[Mode.PHONE_USE],
            geometry={"post_roi": [[0, 0], [1, 0], [1, 1]]},
            options={"behavior_interval_seconds": 660, "phone_use_seconds": 600},
        )


def test_person_confidence_defaults_and_bounds():
    from backend.schemas import CameraOptions
    assert CameraOptions().person_confidence == 0.30
    assert CameraOptions(person_confidence=0.75).person_confidence == 0.75
    with pytest.raises(ValueError):
        CameraOptions(person_confidence=1.01)


def test_camera_frame_interval_supports_one_second():
    camera = CameraCreate(
        id="fast-camera",
        name="高频分析摄像头",
        rtsp_url="rtsp://host/live",
        modes=[Mode.BLACK_SCREEN],
        frame_interval_seconds=1,
    )
    assert camera.frame_interval_seconds == 1
    assert CameraPatch(frame_interval_seconds=1).frame_interval_seconds == 1


def test_display_settings_accept_partial_updates():
    assert DisplaySettingsUpdate(show_traffic_report=False).model_dump(exclude_none=True) == {
        "show_traffic_report": False
    }


def test_camera_batch_limit_and_source_schemes():
    item = {"id": "batch-1", "name": "批量摄像头", "rtsp_url": "rtmp://user:p@host/live"}
    assert len(CameraBatchCreate(items=[item]).items) == 1
    with pytest.raises(ValueError):
        CameraBatchCreate(items=[item] * 501)
    assert CameraBatchDelete(ids=["cam-1", "cam-1", "cam-2"]).ids == ["cam-1", "cam-2"]
    with pytest.raises(ValueError):
        CameraBatchDelete(ids=[])

    camera = CameraCreate(
        id="http-stream",
        name="HTTP 视频源",
        rtsp_url="https://example.invalid/live.m3u8",
        modes=[Mode.BLACK_SCREEN],
    )
    assert camera.rtsp_url.startswith("https://")


def test_model_base_url_allows_http_and_https():
    for url in [
        "http://192.168.1.100:8000/v1",
        "http://172.16.1.20:8080/v1",
        "https://modelrouter.example.com/v1",
        "http://127.0.0.1:11434/v1/",
    ]:
        model = ModelSettingsUpdate(
            provider="openai_compatible", base_url=url, api_key="k",
            economy_model="qwen-vl",
        )
        assert model.base_url == url.rstrip("/")


@pytest.mark.parametrize("bad_url", [
    "ftp://host/v1",
    "file:///etc/x",
    "ws://host/v1",
    "modelrouter.example.com/v1",
    "abc",
    "http://",
])
def test_model_base_url_rejects_non_http(bad_url):
    with pytest.raises(ValueError):
        ModelSettingsUpdate(
            provider="openai_compatible", base_url=bad_url, api_key="k",
            economy_model="qwen-vl",
        )
