import asyncio
import json
from datetime import timedelta
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select, func
from backend import models
from backend.background import Job
from backend.database import utc_now
from test_async_pipeline import db, setup_runtime, threshold

def confirm(runtime, key):
    state = runtime.rules.for_camera(key)
    state.absence_since = utc_now() - timedelta(seconds=61)
    state.absence_alerted = True
    state.record_off_duty_review(True, utc_now(), b'jpeg', .9)

@pytest.mark.asyncio
async def test_early_confirmation_waits_for_all_thresholds(tmp_path, db):
    runtime, post, io, _ = setup_runtime(tmp_path, db)
    confirm(runtime, 'a'); confirm(runtime, 'b')
    runtime.rules.for_camera('b').absence_alerted = False
    assert not await runtime._maybe_create_off_duty_alert(post.repository.get_camera('a'), None, utc_now())
    assert not post.reserved_alerts
    runtime.rules.for_camera('b').absence_alerted = True
    await runtime._maybe_create_off_duty_alert(post.repository.get_camera('a'), None, utc_now())
    assert len(post.reserved_alerts) == 1
    assert runtime.rules.for_camera('a').absence_report_state == 'report_generating'
    assert runtime.rules.for_camera('a').absence_since is not None
    await io.close()

@pytest.mark.asyncio
async def test_final_write_failure_keeps_event_and_allows_retry(tmp_path, db, monkeypatch):
    runtime,post,io,_=setup_runtime(tmp_path,db)
    confirm(runtime,'a');confirm(runtime,'b')
    old=runtime.rules.for_camera('a').absence_since
    await runtime._maybe_create_off_duty_alert(post.repository.get_camera('a'),None,utc_now())
    while post.ingress.pending: await post.ingress.flush_one()
    key=next(iter(post.reserved_alerts))
    complete=post.store.complete
    fail=post.store.fail
    monkeypatch.setattr(post.store,'complete',lambda *args: (_ for _ in ()).throw(RuntimeError('injected write failure')))
    monkeypatch.setattr(post.store,'fail',lambda job,token,error:fail(job,token,error,max_attempts=1))
    monkeypatch.setattr(post,'online',lambda key:True)
    writer=asyncio.create_task(post._writes())
    try:
        for _ in range(200):
            if key not in post.reserved_alerts: break
            await asyncio.sleep(.01)
        assert key not in post.reserved_alerts
        assert runtime.rules.for_camera('a').absence_since==old
        assert runtime.rules.for_camera('a').absence_vlm_confirmed
        assert runtime.rules.for_camera('a').absence_report_state=='report_failed'
        monkeypatch.setattr(post.store,'complete',complete)
        post.alert_retry_after[key]=0
        await runtime._maybe_create_off_duty_alert(post.repository.get_camera('a'),None,utc_now())
        while post.ingress.pending:await post.ingress.flush_one()
        for _ in range(300):
            if runtime.rules.for_camera('a').absence_since!=old: break
            await asyncio.sleep(.01)
        with db() as s:
            assert s.scalar(select(func.count()).select_from(models.Alert))==1
    finally:
        writer.cancel();await asyncio.gather(writer,return_exceptions=True);await io.close()

@pytest.mark.asyncio
async def test_directory_validation_reasons(tmp_path, db):
    runtime, post, io, _ = setup_runtime(tmp_path, db)
    confirm(runtime, 'a'); confirm(runtime, 'b')
    camera = post.cameras['a']
    from backend.async_pipeline import fingerprint
    payload = dict(camera_id='a', camera_fingerprint=fingerprint(camera), mode='off_duty',
                   members={k:runtime.rules.for_camera(k).absence_since.isoformat() for k in ['a','b']})
    assert post.alert_valid(payload)
    runtime.rules.for_camera('b').absence_alerted=False
    assert post.alert_invalid_reason(payload)=='threshold_not_reached:b'
    runtime.rules.for_camera('b').absence_alerted=True
    runtime.media.capture_streams['b'].running=False
    assert post.alert_invalid_reason(payload)=='camera_offline:b'
    runtime.media.capture_streams['b'].running=True
    post.cameras['b']=replace(post.cameras['b'], schedule_json='{"weekly":{"0":[{"start":"00:00","end":"00:01"}]}}')
    assert post.alert_invalid_reason(payload)=='outside_schedule:b'
    await io.close()

@pytest.mark.parametrize('terminal', ['stale','failed'])
@pytest.mark.asyncio
async def test_terminal_event_can_be_rearmed_but_success_cannot(tmp_path, db, terminal):
    runtime, post, io, _ = setup_runtime(tmp_path, db)
    payload=dict(mode='off_duty', directory_id=1,members={'a':'event','b':'event-b'},analysis_job_id='old')
    job=Job.make('alert_write','alert:same-event',payload,'a'); post.store.persist(job)
    with db() as s:
        row=s.get(models.BackgroundTask,job.id)
        row.status='completed' if terminal=='stale' else 'failed'
        row.result_json=json.dumps({'stale':True})
    assert 'alert:same-event' not in post.store.alert_reservations()
    newer=Job.make('alert_write','alert:same-event',{**payload,'analysis_job_id':'new'},'b')
    assert post.store.persist(newer)==job.id
    assert post.store.get(job.id)['status']=='pending'
    with db() as s:
        row=s.get(models.BackgroundTask,job.id);row.status='completed';row.result_json='{"alert_id":123}'
    assert 'alert:same-event' in post.store.alert_reservations()
    with pytest.raises(ValueError):
        post.store.persist(Job.make('alert_write','alert:same-event',payload,'a'))
    await io.close()

@pytest.mark.asyncio
async def test_committed_report_resets_only_matching_events(tmp_path, db):
    runtime, post, io, _ = setup_runtime(tmp_path, db)
    confirm(runtime, 'a'); confirm(runtime, 'b')
    old=runtime.rules.for_camera('a').absence_since
    await runtime._maybe_create_off_duty_alert(post.repository.get_camera('a'),None,utc_now())
    while post.ingress.pending: await post.ingress.flush_one()
    writer=asyncio.create_task(post._writes())
    try:
        for _ in range(150):
            with db() as s:
                if s.scalar(select(func.count()).select_from(models.Alert)) and runtime.rules.for_camera('a').absence_since != old: break
            await asyncio.sleep(.01)
        with db() as s:
            assert s.scalar(select(func.count()).select_from(models.Alert))==1
        assert runtime.rules.for_camera('a').absence_since != old
        assert runtime.rules.for_camera('a').absence_vlm_confirmed is False
        assert len(post.reserved_alerts)==1
    finally:
        writer.cancel();await asyncio.gather(writer,return_exceptions=True);await io.close()

@pytest.mark.asyncio
async def test_stale_write_releases_reservation_and_same_event_retries(tmp_path, db):
    runtime, post, io, _ = setup_runtime(tmp_path, db)
    confirm(runtime,'a');confirm(runtime,'b')
    old=runtime.rules.for_camera('a').absence_since
    await runtime._maybe_create_off_duty_alert(post.repository.get_camera('a'),None,utc_now())
    while post.ingress.pending: await post.ingress.flush_one()
    key=next(iter(post.reserved_alerts))
    runtime.rules.for_camera('b').absence_alerted=False
    writer=asyncio.create_task(post._writes())
    try:
        for _ in range(150):
            if key not in post.reserved_alerts: break
            await asyncio.sleep(.01)
        assert key not in post.reserved_alerts
        assert runtime.rules.for_camera('a').absence_since==old
        assert runtime.rules.for_camera('a').absence_report_state=='report_failed'
        with db() as s:
            row=s.scalar(select(models.BackgroundTask).where(models.BackgroundTask.kind=='alert_write'))
            assert json.loads(row.result_json)['reason']=='threshold_not_reached:b'
            task_id=row.id
        runtime.rules.for_camera('b').absence_alerted=True
        post.alert_retry_after[key]=0
        await runtime._maybe_create_off_duty_alert(post.repository.get_camera('a'),None,utc_now())
        while post.ingress.pending: await post.ingress.flush_one()
        for _ in range(150):
            if runtime.rules.for_camera('a').absence_since!=old: break
            await asyncio.sleep(.01)
        with db() as s:
            assert s.scalar(select(func.count()).select_from(models.Alert))==1
            assert s.get(models.BackgroundTask,task_id).status=='completed'
    finally:
        writer.cancel();await asyncio.gather(writer,return_exceptions=True);await io.close()

@pytest.mark.asyncio
async def test_unknown_review_does_not_confirm_or_submit(tmp_path, db):
    from backend.schemas import Mode,VLMResult
    from backend.vlm import VLMResponse
    runtime,post,io,frame=setup_runtime(tmp_path,db)
    event=threshold(runtime)
    class VLM:
        async def analyze_behaviors(self,*args):
            return VLMResponse(results={Mode.OFF_DUTY:VLMResult(mode=Mode.OFF_DUTY,status='uncertain',confidence=0,reason='unclear')},
                request_id='r',usage={},latency_ms=1,provider='test',model='test')
    runtime.vlm=VLM()
    await runtime._behaviors(post.cameras['a'],{Mode.OFF_DUTY},frame.jpeg,off_duty_event_started_at=event)
    assert not runtime.rules.for_camera('a').absence_vlm_confirmed
    assert not post.reserved_alerts
    runtime.vlm=None
    await runtime._behaviors(post.cameras['a'],{Mode.OFF_DUTY},frame.jpeg,off_duty_event_started_at=event)
    assert not runtime.rules.for_camera('a').absence_vlm_confirmed
    await io.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('expected', ['person_absent_accumulating','person_absent_pending_review',
    'person_absent_waiting_directory','report_generating','report_failed'])
async def test_local_audit_displays_real_report_stage(tmp_path,db,expected):
    runtime,post,io,_=setup_runtime(tmp_path,db)
    threshold(runtime,'a')
    state=runtime.rules.for_camera('a')
    if expected=='person_absent_accumulating':
        state.absence_since=utc_now()-timedelta(seconds=10)
        state.absence_alerted=False
    elif expected!='person_absent_pending_review':
        state.record_off_duty_review(True,utc_now(),b'jpeg',.9)
        if expected.startswith('report_'):state.absence_report_state=expected
    runtime.vlm=None
    runtime.background_io=io
    await runtime._process(post.cameras['a'])
    local=[json.loads(j.payload_json) for j,_ in post.ingress.pending.values()
           if j.kind=='analysis_write' and json.loads(j.payload_json).get('analysis_source')=='local_yolo']
    assert local[-1]['off_duty_state']==expected
    await io.close()

@pytest.mark.asyncio
async def test_generated_audit_and_old_completion_does_not_change_new_event(tmp_path,db):
    runtime,post,io,_=setup_runtime(tmp_path,db)
    confirm(runtime,'a');state=runtime.rules.for_camera('a')
    payload={'members':{'a':state.absence_since.isoformat()}}
    post.report_status(payload,'report_generated','success')
    jobs=[json.loads(j.payload_json) for j,_ in post.ingress.pending.values()]
    assert jobs[-1]['off_duty_state']=='report_generated'
    state.start_new_off_duty_cycle(utc_now())
    new=state.absence_since
    post.report_status(payload,'report_failed','old task failed')
    assert state.absence_since==new and state.absence_report_state is None
    await io.close()
