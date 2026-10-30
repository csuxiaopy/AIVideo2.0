"""Transactional outbox access. Every call owns its session; no ORM escapes."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.exc import IntegrityError

from backend.background import IdempotencyConflict, Job
from backend.database import session_scope, utc_now
from backend.models import BackgroundTask


class TaskStore:
    def __init__(self, sessions=session_scope):
        self.sessions = sessions

    @staticmethod
    def _check(row, job):
        if job.kind == row.kind == "alert_write":
            result = json.loads(row.result_json or "{}")
            previous, current = json.loads(row.payload_json), json.loads(job.payload_json)
            same_event = (
                previous.get("mode") == current.get("mode") == "off_duty"
                and bool(current.get("members"))
                and previous.get("members") == current.get("members")
                and previous.get("directory_id") == current.get("directory_id")
                and job.camera_id in current.get("members", {})
            )
            if same_event and (row.status == "failed" or (row.status == "completed" and (result.get("stale") or result.get("skipped") == "cooldown"))):
                # Revalidate a current event without replaying historical work.
                row.payload_json, row.camera_id, row.evidence_ref = job.payload_json, job.camera_id, job.evidence_ref
                row.status, row.attempts, row.retry_at = "pending", 0, utc_now()
                row.result_json, row.error = None, None
                row.lease_token, row.lease_until, row.updated_at = None, None, utc_now()
                return row.id
        if (row.kind, row.camera_id, row.payload_json, row.evidence_ref) != (
            job.kind, job.camera_id, job.payload_json, job.evidence_ref
        ):
            raise IdempotencyConflict(job, row)
        return row.id

    def alert_reservations(self) -> dict[str, dict]:
        """Return durable alert identities without exposing their payloads."""
        with self.sessions() as session:
            rows = session.execute(select(
                BackgroundTask.idempotency_key, BackgroundTask.id,
                BackgroundTask.status, BackgroundTask.camera_id, BackgroundTask.result_json,
            ).where(BackgroundTask.kind == "alert_write")).all()
            return {key: {"id": task_id, "status": status, "camera_id": camera_id}
                    for key, task_id, status, camera_id, result_json in rows
                    if status in {"pending", "running", "retry"}
                    or (status == "completed" and json.loads(result_json or "{}").get("alert_id"))}

    def prune_terminal(
        self,
        completed_before: datetime,
        failed_before: datetime,
        limit: int = 10000,
    ) -> int:
        """Delete one bounded batch of expired terminal outbox rows."""
        if not 1 <= limit <= 20000:
            raise ValueError("Cleanup batch must contain 1..20000 rows")
        expired = or_(
            and_(BackgroundTask.status == "completed", BackgroundTask.updated_at < completed_before),
            and_(BackgroundTask.status == "failed", BackgroundTask.updated_at < failed_before),
        )
        with self.sessions() as session:
            # Alert writes refer to their analysis outbox row by UUID. Keep that
            # completed dependency until the referring alert reaches a terminal
            # completed state or ages out under the failed-task policy.
            protected_analysis_ids: set[str] = set()
            payloads = session.scalars(select(BackgroundTask.payload_json).where(
                BackgroundTask.kind == "alert_write",
                BackgroundTask.status != "completed",
            ))
            for payload_json in payloads:
                try:
                    analysis_job_id = json.loads(payload_json).get("analysis_job_id")
                except (TypeError, json.JSONDecodeError):
                    continue
                if analysis_job_id:
                    protected_analysis_ids.add(str(analysis_job_id))
            eligible = expired
            if protected_analysis_ids:
                eligible = and_(expired, BackgroundTask.id.not_in(protected_analysis_ids))
            ids = list(session.scalars(
                select(BackgroundTask.id)
                .where(eligible)
                .order_by(BackgroundTask.updated_at, BackgroundTask.id)
                .limit(limit)
            ))
            if not ids:
                return 0
            result = session.execute(
                delete(BackgroundTask).where(BackgroundTask.id.in_(ids), eligible),
                execution_options={"synchronize_session": False},
            )
            return int(result.rowcount or 0)

    def persist(self, job: Job) -> str:
        try:
            with self.sessions() as session:
                previous = session.scalar(select(BackgroundTask).where(BackgroundTask.idempotency_key == job.key).with_for_update())
                if previous:
                    return self._check(previous, job)
                session.add(BackgroundTask(
                    id=job.id, idempotency_key=job.key, kind=job.kind,
                    camera_id=job.camera_id, payload_json=job.payload_json,
                    evidence_ref=job.evidence_ref, status="pending", attempts=0,
                    retry_at=utc_now(), created_at=utc_now(), updated_at=utc_now(),
                ))
            return job.id
        except IntegrityError:
            # Includes the concurrent-INSERT race; only identical work is deduped.
            with self.sessions() as session:
                previous = session.scalar(select(BackgroundTask).where(BackgroundTask.idempotency_key == job.key))
                if previous is None:
                    raise
                return self._check(previous, job)

    def persist_many(self, jobs: list[Job]) -> list[str]:
        if not jobs or len(jobs) > 100:
            raise ValueError("Batch must contain 1..100 jobs")
        try:
            with self.sessions() as session:
                existing = {row.idempotency_key: row for row in session.scalars(select(BackgroundTask).where(
                    BackgroundTask.idempotency_key.in_([job.key for job in jobs])).with_for_update())}
                ids = []
                for job in jobs:
                    if job.key in existing:
                        ids.append(self._check(existing[job.key], job))
                    else:
                        row = BackgroundTask(id=job.id, idempotency_key=job.key, kind=job.kind,
                            camera_id=job.camera_id, payload_json=job.payload_json, evidence_ref=job.evidence_ref,
                            status="pending", attempts=0, retry_at=utc_now(), created_at=utc_now(), updated_at=utc_now())
                        session.add(row)
                        existing[job.key] = row
                        ids.append(job.id)
                return ids
        except IntegrityError:
            return [self.persist(job) for job in jobs]

    def claim_many(self, kinds: list[str], limit: int = 100, lease_seconds: int = 120) -> list[dict]:
        if not kinds or not 1 <= limit <= 100 or lease_seconds < 1:
            raise ValueError("Invalid claim bounds")
        now = utc_now()
        eligible = or_(
            and_(BackgroundTask.status.in_(["pending", "retry"]), BackgroundTask.retry_at <= now),
            and_(BackgroundTask.status == "running", BackgroundTask.lease_until <= now),
        )
        with self.sessions() as session:
            rows = list(session.scalars(select(BackgroundTask).where(BackgroundTask.kind.in_(kinds), eligible)
                .order_by(BackgroundTask.created_at, BackgroundTask.id).limit(limit).with_for_update(skip_locked=True)))
            result = []
            for row in rows:
                row.status, row.lease_token = "running", str(uuid4())
                row.lease_until, row.updated_at = now + timedelta(seconds=lease_seconds), now
                row.attempts += 1
                result.append({"id":row.id, "key":row.idempotency_key, "kind":row.kind,
                    "camera_id":row.camera_id, "payload_json":row.payload_json, "lease_token":row.lease_token})
            return result

    def complete_many(self, claims: list[dict], handler) -> int:
        if not claims or len(claims) > 100:
            raise ValueError("Invalid completion batch")
        now = utc_now()
        with self.sessions() as session:
            rows = {row.id: row for row in session.scalars(select(BackgroundTask).where(
                BackgroundTask.id.in_([claim["id"] for claim in claims])).with_for_update())}
            completed = 0
            for claim in claims:
                row = rows.get(claim["id"])
                if (row is None or row.status != "running" or row.lease_token != claim["lease_token"]
                    or row.lease_until is None or row.lease_until.replace(tzinfo=now.tzinfo) <= now):
                    continue
                result = handler(session, row.kind, json.loads(row.payload_json))
                row.status, row.result_json = "completed", json.dumps(result, ensure_ascii=False)
                row.lease_token, row.lease_until, row.error, row.updated_at = None, None, None, now
                completed += 1
            return completed

    def claim(self, kinds: list[str], lease_seconds: int = 120) -> dict | None:
        if not kinds or lease_seconds < 1:
            raise ValueError("Kinds and positive lease are required")
        now = utc_now()
        eligible = or_(
            and_(BackgroundTask.status.in_(["pending", "retry"]), BackgroundTask.retry_at <= now),
            and_(BackgroundTask.status == "running", BackgroundTask.lease_until <= now),
        )
        with self.sessions() as session:
            row = session.scalar(select(BackgroundTask).where(
                BackgroundTask.kind.in_(kinds), eligible,
            ).order_by(BackgroundTask.created_at, BackgroundTask.id).limit(1).with_for_update(skip_locked=True))
            if row is None:
                return None
            token = str(uuid4())
            # CAS also protects SQLite where FOR UPDATE is ignored.
            updated = session.execute(update(BackgroundTask).where(
                BackgroundTask.id == row.id, eligible,
            ).values(status="running", lease_token=token,
                     lease_until=now + timedelta(seconds=lease_seconds),
                     attempts=BackgroundTask.attempts + 1, updated_at=now),
                execution_options={"synchronize_session": False})
            if updated.rowcount != 1:
                return None
            return {"id": row.id, "key": row.idempotency_key, "kind": row.kind,
                    "camera_id": row.camera_id, "payload_json": row.payload_json,
                    "evidence_ref": row.evidence_ref, "lease_token": token}

    def renew(self, job_id: str, token: str, seconds: int = 120) -> bool:
        if seconds < 1:
            raise ValueError("Positive lease required")
        now = utc_now()
        with self.sessions() as session:
            return session.execute(update(BackgroundTask).where(
                BackgroundTask.id == job_id, BackgroundTask.status == "running",
                BackgroundTask.lease_token == token, BackgroundTask.lease_until > now,
            ).values(lease_until=now + timedelta(seconds=seconds), updated_at=now)).rowcount == 1

    def complete(self, job_id: str, token: str, handler) -> bool:
        """Commit DB effects and task completion in ONE transaction.

        The handler accepts (session, parsed payload), returns JSON, and must not
        perform external I/O. Evidence is written before this operation; external
        notifications require a separate outbox row inserted by the handler.
        """
        with self.sessions() as session:
            row = session.scalar(select(BackgroundTask).where(
                BackgroundTask.id == job_id,
            ).with_for_update())
            if row is None or row.status != "running" or row.lease_token != token:
                return False
            # SQLite strips tzinfo; compare timestamps uniformly.
            now = utc_now()
            deadline = row.lease_until
            if deadline is None or deadline.replace(tzinfo=now.tzinfo) <= now:
                return False
            result = handler(session, json.loads(row.payload_json))
            row.result_json = json.dumps(result, ensure_ascii=False, allow_nan=False)
            row.status, row.error = "completed", None
            row.lease_token, row.lease_until = None, None
            row.updated_at = now
            return True

    def fail(self, job_id: str, token: str, error: str, max_attempts: int = 5) -> bool:
        if max_attempts < 1:
            raise ValueError("Positive attempt limit required")
        with self.sessions() as session:
            row = session.scalar(select(BackgroundTask).where(BackgroundTask.id == job_id).with_for_update())
            if row is None or row.status != "running" or row.lease_token != token:
                return False
            now = utc_now()
            if row.lease_until is None or row.lease_until.replace(tzinfo=now.tzinfo) <= now:
                return False
            row.status = "failed" if row.attempts >= max_attempts else "retry"
            row.error = error[:1000]
            row.retry_at = utc_now() + timedelta(seconds=min(300, 2 ** min(row.attempts, 8)))
            row.updated_at = utc_now()
            row.lease_token, row.lease_until = None, None
            return True

    def retry(self, job_id: str) -> bool:
        with self.sessions() as session:
            return session.execute(update(BackgroundTask).where(
                BackgroundTask.id == job_id, BackgroundTask.status == "failed",
            ).values(status="pending", attempts=0, retry_at=utc_now(), updated_at=utc_now(),
                     lease_token=None, lease_until=None, error=None)).rowcount == 1

    def get(self, job_id: str) -> dict | None:
        with self.sessions() as session:
            row = session.get(BackgroundTask, job_id)
            if row is None:
                return None
            # Do not expose internal payload/evidence/webhook credentials.
            return {"id": row.id, "camera_id": row.camera_id, "kind": row.kind,
                    "status": row.status, "attempts": row.attempts,
                    "created_at": row.created_at.isoformat(),
                    "updated_at": row.updated_at.isoformat(),
                    "result": json.loads(row.result_json) if row.result_json else None,
                    "error": row.error}
