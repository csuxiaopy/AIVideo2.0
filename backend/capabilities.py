from __future__ import annotations

from copy import deepcopy
from typing import Any

from backend.schemas import CameraOptions, GeometrySpec, Mode, SceneType, ScheduleSpec


CORE_CAPABILITIES: dict[str, dict[str, Any]] = {
    Mode.OFF_DUTY.value: {"name": "离岗检测", "availability": "ready", "severity": "normal"},
    Mode.PHONE_USE.value: {"name": "玩手机检测", "availability": "ready", "severity": "normal"},
    Mode.PEOPLE_FLOW.value: {"name": "人员计数", "availability": "ready", "severity": "info"},
    Mode.FIRE_SMOKE.value: {"name": "烟火检测", "availability": "ready", "severity": "critical"},
    Mode.INTRUSION.value: {"name": "区域入侵", "availability": "ready", "severity": "high"},
    Mode.BLACK_SCREEN.value: {"name": "屏幕黑屏", "availability": "ready", "severity": "high"},
    Mode.ON_DUTY.value: {"name": "在岗判定", "availability": "experimental", "severity": "info"},
    Mode.SMOKING.value: {"name": "人员吸烟", "availability": "experimental", "severity": "normal"},
}

PLANNED_CAPABILITIES = [
    {"id": "workwear", "name": "工服识别", "availability": "planned"},
    {"id": "abandoned_object", "name": "物品遗留", "availability": "planned"},
    {"id": "crowd", "name": "人员聚集", "availability": "planned"},
    {"id": "sleeping", "name": "睡岗检测", "availability": "planned"},
    {"id": "passage_blockage", "name": "通道堵塞", "availability": "planned"},
]


def _weekday_schedule(shifts: list[dict[str, str]], days: range) -> dict[str, Any]:
    return {
        "timezone": "Asia/Shanghai",
        "weekly": {str(day): deepcopy(shifts) for day in days},
        "holidays": [],
    }


SCENE_TEMPLATES: dict[SceneType, dict[str, Any]] = {
    SceneType.WORKSTATION: {
        "name": "员工工位",
        "description": "员工行为规范与离岗管理",
        "modes": [Mode.OFF_DUTY.value, Mode.PHONE_USE.value, Mode.BLACK_SCREEN.value],
        "schedule": _weekday_schedule(
            [{"start": "09:30", "end": "11:00"}, {"start": "14:00", "end": "17:00"}], range(5)
        ),
        "required_geometry": ["post_roi"],
    },
    SceneType.CUSTOMER_AREA: {
        "name": "客户位/入口",
        "description": "营业厅客流统计",
        "modes": [Mode.PEOPLE_FLOW.value, Mode.BLACK_SCREEN.value],
        "schedule": _weekday_schedule([{"start": "08:30", "end": "17:30"}], range(7)),
        "required_geometry": ["flow_line"],
    },
    SceneType.SECURITY_AREA: {
        "name": "库房/全局区域",
        "description": "全天烟火与禁区安全检测",
        "modes": [Mode.FIRE_SMOKE.value, Mode.INTRUSION.value, Mode.BLACK_SCREEN.value],
        "schedule": {"timezone": "Asia/Shanghai", "weekly": {}, "holidays": []},
        "required_geometry": ["intrusion_zone"],
    },
}


ALWAYS_ON_MODES = {Mode.FIRE_SMOKE.value, Mode.INTRUSION.value, Mode.BLACK_SCREEN.value}


def scene_templates_public() -> list[dict[str, Any]]:
    options = CameraOptions().model_dump()
    geometry = GeometrySpec().model_dump()
    templates = [
        {
            "scene_type": scene.value,
            **deepcopy(template),
            "options": deepcopy(options),
            "geometry": deepcopy(geometry),
        }
        for scene, template in SCENE_TEMPLATES.items()
    ]
    for template in templates:
        if template["scene_type"] == SceneType.WORKSTATION.value:
            template["geometry"]["post_roi"] = [[0, 0], [1, 0], [1, 1], [0, 1]]
    return templates


def capabilities_public() -> list[dict[str, Any]]:
    return [{"id": mode, **detail} for mode, detail in CORE_CAPABILITIES.items()] + deepcopy(PLANNED_CAPABILITIES)


def mode_is_active(mode: str, schedule: ScheduleSpec, now=None) -> bool:
    if mode in ALWAYS_ON_MODES:
        return True
    from backend.rules import is_scheduled

    return is_scheduled(schedule, now)
