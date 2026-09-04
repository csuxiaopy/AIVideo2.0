from datetime import datetime, timezone

from backend.capabilities import ALWAYS_ON_MODES, mode_is_active, scene_templates_public
from backend.schemas import Mode, ScheduleSpec


def test_scene_templates_have_expected_modes_and_geometry():
    templates = {item["scene_type"]: item for item in scene_templates_public()}
    assert set(templates) == {"workstation", "customer_area", "security_area"}
    assert templates["workstation"]["modes"] == ["off_duty", "phone_use", "black_screen"]
    assert templates["workstation"]["required_geometry"] == ["post_roi"]
    assert templates["workstation"]["geometry"]["post_roi"] == [[0, 0], [1, 0], [1, 1], [0, 1]]
    assert templates["workstation"]["schedule"]["weekly"]["0"] == [
        {"start": "09:30", "end": "11:00"},
        {"start": "14:00", "end": "17:00"},
    ]
    assert templates["workstation"]["options"]["fire_confidence"] == 0.3
    assert templates["workstation"]["options"]["smoke_confidence"] == 0.3
    assert templates["customer_area"]["required_geometry"] == ["flow_line"]
    assert templates["security_area"]["required_geometry"] == ["intrusion_zone"]
    assert set(templates["security_area"]["modes"]) == {"fire_smoke", "intrusion", "black_screen"}


def test_safety_modes_ignore_closed_schedule():
    closed = ScheduleSpec.model_validate({
        "timezone": "UTC",
        "weekly": {"0": [{"start": "08:30", "end": "09:00"}]},
    })
    noon = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    assert not mode_is_active(Mode.OFF_DUTY.value, closed, noon)
    for mode in ALWAYS_ON_MODES:
        assert mode_is_active(mode, closed, noon)
