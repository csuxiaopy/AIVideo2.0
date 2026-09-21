"""Inspect and safely recover durable background tasks.

The default mode is read-only. Marking old failed alerts stale never sends a
webhook. Retrying an alert can send it and therefore requires an explicit risk
acknowledgement in addition to --apply.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from backend import models  # noqa: E402
from backend.database import session_scope, utc_now  # noqa: E402
from backend.task_store import TaskStore  # noqa: E402


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def failed_alerts(before: datetime | None = None):
    with session_scope() as session:
        query = select(models.BackgroundTask).where(
            models.BackgroundTask.kind == "alert_write",
            models.BackgroundTask.status == "failed",
        ).order_by(models.BackgroundTask.created_at, models.BackgroundTask.id)
        if before is not None:
            query = query.where(models.BackgroundTask.created_at < before)
        return [{
            "id": row.id, "camera_id": row.camera_id, "status": row.status,
            "attempts": row.attempts, "error": row.error,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        } for row in session.scalars(query)]


def mark_stale(before: datetime, reason: str) -> int:
    changed = 0
    with session_scope() as session:
        rows = session.scalars(select(models.BackgroundTask).where(
            models.BackgroundTask.kind == "alert_write",
            models.BackgroundTask.status == "failed",
            models.BackgroundTask.created_at < before,
        ).with_for_update()).all()
        for row in rows:
            row.result_json = json.dumps({
                "stale": True, "recovery": "operator_marked_stale",
                "reason": reason, "previous_error": row.error,
                "recovered_at": utc_now().isoformat(),
            }, ensure_ascii=False)
            row.status = "completed"
            row.error = None
            row.lease_token = None
            row.lease_until = None
            row.updated_at = utc_now()
            changed += 1
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=parse_time,
                        help="only inspect/change failed alerts created before this ISO timestamp")
    parser.add_argument("--mark-stale", action="store_true",
                        help="mark matching failed alerts completed/stale without sending them")
    parser.add_argument("--reason", default="",
                        help="required audit reason for --mark-stale")
    parser.add_argument("--retry-task", help="retry one still-valid failed alert task by UUID")
    parser.add_argument("--allow-old-alert-delivery", action="store_true",
                        help="acknowledge that --retry-task can send an old webhook")
    parser.add_argument("--apply", action="store_true",
                        help="apply the selected change; omission is always read-only")
    args = parser.parse_args()

    if args.mark_stale:
        if not args.before or not args.reason.strip():
            parser.error("--mark-stale requires --before and a non-empty --reason")
        rows = failed_alerts(args.before)
        if not args.apply:
            print(json.dumps({"dry_run": True, "action": "mark_stale", "matches": rows}, ensure_ascii=False))
            return
        print(json.dumps({"dry_run": False, "marked_stale": mark_stale(args.before, args.reason.strip())}))
        return

    if args.retry_task:
        if not args.apply or not args.allow_old_alert_delivery:
            parser.error("retry requires --apply and --allow-old-alert-delivery")
        with session_scope() as session:
            row = session.get(models.BackgroundTask, args.retry_task)
            if row is None or row.kind != "alert_write" or row.status != "failed":
                parser.error("task must be an existing failed alert_write")
        print(json.dumps({"retried": TaskStore().retry(args.retry_task), "task_id": args.retry_task}))
        return

    print(json.dumps({"dry_run": True, "failed_alerts": failed_alerts(args.before)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
