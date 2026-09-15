"""Read-only rolling capture/analysis sample. Run inside the app container."""
import json
import time
import urllib.request
from collections import Counter


def snapshot():
    started = time.monotonic()
    with urllib.request.urlopen("http://127.0.0.1:8100/health", timeout=25) as response:
        data = json.load(response)
    return time.monotonic(), data, time.monotonic() - started


previous_time, previous, _ = snapshot()
for sample in range(3):
    time.sleep(20)
    now, current, latency = snapshot()
    captures = current["media"]["captures"]
    before = previous["media"]["captures"]
    groups = {}
    for decoder in ("cuda", "cpu"):
        rows = [(key, row) for key, row in captures.items() if row.get("decoder") == decoder]
        rates = sorted((row["frames"] - before[key]["frames"]) / (now - previous_time)
                       for key, row in rows if key in before and row["frames"] > 0)
        ages = sorted(row["latest_frame_age_seconds"] for _, row in rows
                      if row["latest_frame_age_seconds"] is not None)
        groups[decoder] = {
            "configured": len(rows), "with_frames": len(ages),
            "fps_min": round(min(rates), 3) if rates else None,
            "fps_median": round(rates[len(rates) // 2], 3) if rates else None,
            "fps_max": round(max(rates), 3) if rates else None,
            "age_p95": ages[min(len(ages) - 1, int(len(ages) * .95))] if ages else None,
            "new_reconnects": sum(row["reconnects"] - before[key]["reconnects"]
                                  for key, row in rows if key in before),
        }
    print(json.dumps({
        "sample": sample + 1, "elapsed": round(now - previous_time, 2),
        "health_seconds": round(latency, 3), "capture": groups,
        "gpu_assignment": dict(Counter(row.get("decode_device") for row in captures.values())),
        "tasks_per_second": round((current["workers"]["processed"] - previous["workers"]["processed"]) / (now - previous_time), 3),
        "workers": current["workers"], "scheduler": current["scheduler"],
        "queues": current["queues"],
    }), flush=True)
    previous_time, previous = now, current
