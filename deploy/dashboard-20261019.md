# Dashboard query and cache optimization

The former dashboard hydrated all 128,230 traffic rows into Python before
selecting the latest count per camera. The new implementation returns the sum
of indexed latest-count lookups directly from the database and merges related
aggregations, reducing ten statements to five. It preserves the existing UTC
day boundaries, camera scope (including disabled cameras), response fields and
empty-table behavior.

Migration `20261019_22` adds a partial date index on analyses with a non-null
error. Migration `20261019_23` adds the covering latest-count index
`(camera_id, bucket_start DESC) INCLUDE (current_count)`. Both build concurrently
on PostgreSQL. Existing date indexes remain in use; the existing unique bucket
constraint remains unchanged. Execution-plan inspection showed the planner
preferred a slow global-time scan without the covering index.

`DashboardCache` stores summaries for five seconds. Redis shares results and a
bounded refresh lock across Web workers and analysis. A process-local lock and
cache prevent duplicate work within each process; Redis errors fall back to
database loading and local caching with a 30-second Redis retry backoff. Cache
keys include the database namespace and UTC date. Local cache hits cannot extend
the Redis freshness interval, and returned nested objects are copied. Runtime
status, authentication, cookies and individual-user responses are not cached.

Snapshot verification on production data returned identical fields: old
repository ~2.753 seconds, new uncached repository ~0.028 seconds. Latest-count
SQL used 82 indexed seeks and completed in ~2.291 ms. These are measured samples,
not fixed latency guarantees.

Server backup: `/data/yolo_vlm_monitor/.deploy-backup-20261019-dashboard/`.
Previous image: `yolo-vlm-gpu:before-dashboard-20261019`.
New image: `yolo-vlm-gpu:dashboard-20261019`, also tagged `production`.
The split Compose overlay now supplies `REDIS_URL` to Web as well as analysis.
Rollback can restore the original repository/overlay and previous image tag,
then recreate `app web` using `sh deploy/split.sh up -d --no-deps --no-build app web`.
The additive indexes can remain installed during an application rollback.
