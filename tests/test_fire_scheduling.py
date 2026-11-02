import asyncio
from collections import defaultdict
from types import SimpleNamespace
from datetime import datetime,timezone
import pytest
from backend.pipeline import MonitoringRuntime
from backend.rules import RuleStateRegistry


def runtime():
 r=object.__new__(MonitoringRuntime)
 camera=SimpleNamespace(id="a",enabled=True,modes_json='["fire_smoke"]',options_json='{}')
 frame=SimpleNamespace(sequence=1,jpeg=b"test",captured_at=datetime.now(timezone.utc))
 rows=[];calls=[]
 r.repository=SimpleNamespace(get_camera=lambda key:camera,add_analysis=lambda **kw:rows.append(kw))
 r.media=SimpleNamespace(latest=lambda key:frame,set_safety_detections=lambda *args:None)
 r.fire_locks=defaultdict(asyncio.Lock);r.camera_locks=defaultdict(asyncio.Lock)
 r.fire_config_signatures={};r.fire_last_sample_at={};r.last_fire_sequence=defaultdict(int)
 r.rules=RuleStateRegistry();r.last_detector_error=defaultdict(float)
 def detect(jpeg):calls.append(jpeg);return []
 r.fire_smoke=SimpleNamespace(detect=detect,available=True,version="test",last_latency_ms=1)
 return r,camera,frame,rows,calls


@pytest.mark.asyncio
async def test_fire_does_not_wait_for_general_camera_lock():
 r,c,f,rows,calls=runtime()
 await r.camera_locks[c.id].acquire()
 try:await asyncio.wait_for(r._process_fire(c),1)
 finally:r.camera_locks[c.id].release()
 assert len(calls)==1


@pytest.mark.asyncio
async def test_same_frame_cannot_count_twice_even_when_forced():
 r,c,f,rows,calls=runtime()
 await r._process_fire(c)
 result=await r._process_fire(c,force=True)
 assert len(calls)==1 and result['skipped']=='duplicate_or_missing_frame'


@pytest.mark.asyncio
async def test_busy_fire_skips_without_blocking_other_work():
 r,c,f,rows,calls=runtime()
 await r.fire_locks[c.id].acquire()
 try:result=await asyncio.wait_for(r._process_fire(c),1)
 finally:r.fire_locks[c.id].release()
 assert result['skipped']=='fire_busy' and not calls


@pytest.mark.asyncio
async def test_invalid_confidence_does_not_confirm_or_advance_temporal_history():
 r,c,f,rows,calls=runtime()
 r.fire_smoke.detect=lambda jpeg:[SimpleNamespace(confidence=769.,class_name='fire')]
 result=await r._process_fire(c)
 assert 'detector_error' in result
 assert rows[0]['status']=='uncertain'
 assert r.rules.for_camera(c.id).fire_consecutive==0
 assert not r.rules.for_camera(c.id).smoke_window
 assert r.last_fire_sequence[c.id]==1


@pytest.mark.asyncio
async def test_configuration_change_discards_inflight_result():
 r,c,f,rows,calls=runtime()
 def detect(jpeg):c.options_json='{"fire_confidence":0.99}';return []
 r.fire_smoke.detect=detect
 result=await r._process_fire(c)
 assert result['skipped']=='stale_configuration' and not rows


def test_fixed_schedule_skips_missed_periods_without_drift():
 r=object.__new__(MonitoringRuntime);r.next_fire_run={"a":100.}
 r._advance_fire_due("a",102.5)
 assert r.next_fire_run["a"]==110.
 r._advance_fire_due("a",145.)
 assert r.next_fire_run["a"]==150.


@pytest.mark.asyncio
async def test_old_image_is_not_used_for_temporal_confirmation():
 from datetime import timedelta
 r,c,f,rows,calls=runtime();f.captured_at-=timedelta(minutes=1)
 result=await r._process_fire(c)
 assert result['skipped']=='stale_frame' and not calls
