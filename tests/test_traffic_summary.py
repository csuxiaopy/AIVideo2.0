from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

from backend.repository import Repository


def _camera(camera_id: str, name: str, modes: str = '["people_flow"]', online: bool = True):
    return SimpleNamespace(id=camera_id, name=name, modes_json=modes, online=online)


def _row(camera_id: str, timestamp: datetime, current: int, entered: int = 0, exited: int = 0):
    return SimpleNamespace(
        camera_id=camera_id, bucket_start=timestamp, current_count=current,
        entered=entered, exited=exited,
    )


def test_traffic_summary_uses_shanghai_day_and_carries_camera_state(monkeypatch):
    cameras = [_camera("cam-a", "一号门"), _camera("cam-b", "二号门"), _camera("other", "仓库", "[]")]
    # 2026-09-01 16:00 UTC is 2026-09-02 00:00 in Shanghai.
    prior = {
        "cam-a": _row("cam-a", datetime(2026, 9, 1, 15, 59, tzinfo=timezone.utc), 2),
        "cam-b": None,
    }
    today = [
        _row("cam-a", datetime(2026, 9, 1, 16, 1, tzinfo=timezone.utc), 3, entered=1),
        _row("cam-b", datetime(2026, 9, 1, 16, 2, tzinfo=timezone.utc), 4, entered=3, exited=1),
        _row("cam-a", datetime(2026, 9, 1, 16, 3, tzinfo=timezone.utc), 5, entered=2),
    ]

    class FakeSession:
        scalars_index = 0

        def scalars(self, _statement):
            result = [cameras, today, [prior["cam-a"]]][self.scalars_index]
            self.scalars_index += 1
            return result

    @contextmanager
    def fake_scope():
        yield FakeSession()

    monkeypatch.setattr("backend.repository.session_scope", fake_scope)
    result = Repository().traffic_summary(datetime(2026, 9, 2, 4, 0, tzinfo=timezone.utc))

    assert result["date"] == "2026-09-02"
    assert result["timezone"] == "Asia/Shanghai"
    assert result["flow_camera_count"] == 2
    assert result["total_flow_today"] == result["entered_today"] == 6
    assert result["exited_today"] == 1
    assert result["current_people"] == 9
    assert [point["current_people"] for point in result["store_trend"]] == [3, 7, 9]
    # Equal daily flow is resolved by camera ID for stable podium ordering.
    assert [item["camera_id"] for item in result["flow_ranking"]] == ["cam-a", "cam-b"]
    assert [item["camera_id"] for item in result["current_ranking"]] == ["cam-a", "cam-b"]


def test_traffic_summary_returns_empty_dashboard_without_flow_cameras(monkeypatch):
    class FakeSession:
        def scalars(self, _statement):
            return [_camera("other", "仓库", "[]")]

    @contextmanager
    def fake_scope():
        yield FakeSession()

    monkeypatch.setattr("backend.repository.session_scope", fake_scope)
    result = Repository().traffic_summary(datetime(2026, 9, 2, tzinfo=timezone.utc))
    assert result["flow_camera_count"] == 0
    assert result["store_trend"] == []
    assert result["cameras"] == []
    assert result["current_ranking"] == []
