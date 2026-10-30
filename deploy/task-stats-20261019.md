# Background task completion statistics

Deployed on 2026-10-19 to 172.16.166.229. Migration `20261019_21`:

- Adds `background_task_completions`, one completion watermark per task kind.
- A database trigger updates it in the task's transaction. Failed/rolled-back
  transactions cannot advance it; out-of-order completions cannot regress it.
- Bootstraps historical watermarks once using latest-row index lookups per kind.
- Adds concurrent PostgreSQL indexes for completed latest-row lookups and
  unfinished-task aggregation. SQLite uses equivalent regular indexes/triggers.
- Caches completion watermarks for 15 seconds in `CompletionStats`. Failure and
  unfinished-task checks continue every maintenance iteration (about one second).
- Watermarks survive task retention cleanup and application restarts. They now
  mean latest observed successful completion, including already-pruned tasks.

Observed database execution time: former grouped historical scan ~1579.9 ms;
summary-table read ~0.124 ms; unfinished aggregation ~0.180 ms in an idle-queue
sample. These are query measurements, not whole-page timing guarantees.

Server backup: `/data/yolo_vlm_monitor/.deploy-backup-20261019-task-stats/`.
Original image: `yolo-vlm-gpu:before-task-stats-20261019`.
Patched image: `yolo-vlm-gpu:task-stats-20261019`, also tagged `production`.
The host sources and running container have the same narrowly applied changes;
the local checkout's unrelated existing edits were not deployed.

Application rollback: restore `backend/async_pipeline.py` and `backend/models.py`
from the backup's `host` / `running` directories, restore the original image tag,
and restart the app. The additive database migration can remain installed. If
removing it, roll back application code first, then run Alembic downgrade to
`20260929_20` in a maintenance window.
