"""PostgreSQL-only transactional outbox checks.

Run with TEST_POSTGRES_URL set to an isolated disposable database. The normal
suite skips this module so it can never modify a developer or production DB by
accident.
"""
import os
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.background import CriticalIngress, IOPool, Job
from backend.models import BackgroundTask
from backend.task_store import TaskStore


@pytest.fixture
def postgres_store():
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    engine = create_engine(url)
    BackgroundTask.__table__.drop(engine, checkfirst=True)
    BackgroundTask.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def sessions():
        with factory.begin() as session:
            yield session

    try:
        yield TaskStore(sessions)
    finally:
        BackgroundTask.__table__.drop(engine, checkfirst=True)
        engine.dispose()


@pytest.mark.asyncio
async def test_postgres_batch_conflict_isolated_without_blocking_valid_jobs(postgres_store):
    old = Job.make("alert_write", "alert:postgres-event", {"analysis": "old"}, "a")
    postgres_store.persist(old)
    jobs = [
        Job.make("analysis_write", "analysis:before", {"value": 1}, "b"),
        Job.make("alert_write", old.key, {"analysis": "new"}, "a"),
        Job.make("model_log_write", "model:after", {"value": 2}, "b"),
    ]
    io = IOPool(2)
    queue = CriticalIngress(io, postgres_store.persist, 10)
    queue.persist_batch = postgres_store.persist_many
    assert all(queue.submit(job) for job in jobs)
    assert await queue.flush_one()
    assert not queue.pending
    assert postgres_store.get(jobs[0].id)["status"] == "pending"
    assert postgres_store.get(jobs[2].id)["status"] == "pending"
    assert queue.status()["idempotency_conflicts"] == 1
    await io.close()
