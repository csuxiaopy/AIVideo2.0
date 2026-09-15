"""Sample production batch efficiency and per-mode detection rates."""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
import urllib.request
from datetime import datetime, timezone


def percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(len(ordered) * ratio) - 1))]


def gpu_snapshot() -> list[dict]:
    try:
        output = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
            text=True, timeout=10,
        )
        return [
            {"index": int(row[0]), "utilization_percent": float(row[1]), "memory_mib": float(row[2])}
            for line in output.splitlines() if line.strip()
            for row in [[item.strip() for item in line.split(",")]]
        ]
    except Exception as exc:
        return [{"error": f"{type(exc).__name__}: {exc}"}]


parser = argparse.ArgumentParser()
parser.add_argument("--samples", type=int, default=45)
parser.add_argument("--interval", type=float, default=20)
parser.add_argument("--label", required=True)
args = parser.parse_args()
rows = []

for index in range(args.samples):
    if index:
        time.sleep(args.interval)
    with urllib.request.urlopen("http://127.0.0.1:8100/health", timeout=30) as response:
        health = json.load(response)
    modes = {}
    frame_ages = []
    for mode, cameras in health.get("mode_detection", {}).items():
        rates = [item["frequency_hz"] for item in cameras.values() if item.get("frequency_hz") is not None]
        ages = [item["last_frame_age_seconds"] for item in cameras.values() if item.get("last_frame_age_seconds") is not None]
        frame_ages.extend(ages)
        modes[mode] = {
            "count": len(rates), "min_hz": min(rates) if rates else None,
            "median_hz": statistics.median(rates) if rates else None,
            "p95_hz": percentile(rates, .95),
        }
    row = {
        "label": args.label, "sample": index,
        "at": datetime.now(timezone.utc).isoformat(), "modes": modes,
        "general_queue": health["queues"]["general"],
        "failures": health["workers"]["failures"],
        "frame_age_p95": percentile(frame_ages, .95),
        "batch": health["detectors"]["general"].get("batch_metrics", {}),
        "gpu": gpu_snapshot(),
    }
    rows.append(row)
    print(json.dumps(row, ensure_ascii=False), flush=True)

flow = [row["modes"].get("people_flow", {}).get("median_hz") for row in rows]
flow = [value for value in flow if value is not None]
ages = [row["frame_age_p95"] for row in rows if row["frame_age_p95"] is not None]
queues = [row["general_queue"] for row in rows]
gpu0 = [gpu["utilization_percent"] for row in rows for gpu in row["gpu"] if gpu.get("index") == 0]
summary = {
    "label": args.label, "samples": len(rows),
    "people_flow_median_hz": statistics.median(flow) if flow else None,
    "frame_age_p95": percentile(ages, .95),
    "queue_start": queues[0] if queues else None, "queue_end": queues[-1] if queues else None,
    "queue_max": max(queues) if queues else None,
    "failure_delta": rows[-1]["failures"] - rows[0]["failures"] if rows else None,
    "gpu0_median_percent": statistics.median(gpu0) if gpu0 else None,
    "batch_final": rows[-1]["batch"] if rows else {},
}
print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
