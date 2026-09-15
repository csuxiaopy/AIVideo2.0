"""Read-only post-deployment sampling, run inside the application container."""
import argparse
import json
import time
import urllib.request

from backend.database import utc_now
from backend.repository import Repository, from_json
from backend.rules import is_scheduled
from backend.schemas import ScheduleSpec

parser = argparse.ArgumentParser()
parser.add_argument("--samples", type=int, default=9)
parser.add_argument("--interval", type=float, default=20)
args = parser.parse_args()
cameras = Repository().list_cameras()
for index in range(args.samples):
    if index:
        time.sleep(args.interval)
    with urllib.request.urlopen("http://127.0.0.1:8100/health", timeout=30) as response:
        data = json.load(response)
    persons = data.get("person_detection", {})
    modes = {}
    for mode in ("off_duty", "people_flow"):
        eligible = [c.id for c in cameras if c.enabled and mode in from_json(c.modes_json, [])
                    and is_scheduled(ScheduleSpec.model_validate(from_json(c.schedule_json, {})), utc_now())]
        rates = sorted(persons[c]["frequency_hz"] for c in eligible if c in persons
                       and persons[c]["frequency_hz"] is not None)
        modes[mode] = {"configured_active": len(eligible), "measured": len(rates),
                       "hz_min": min(rates) if rates else None,
                       "hz_median": rates[len(rates)//2] if rates else None,
                       "hz_max": max(rates) if rates else None,
                       "ge_095": sum(rate >= .95 for rate in rates)}
    captures = data["media"]["captures"]
    post = data["postprocessing"]
    post.pop("review_last_result", None)
    print(json.dumps({"sample":index, "at":utc_now().isoformat(), "modes":modes,
        "postprocessing":post, "capture_persistence":data["capture_persistence"],
        "with_frames":sum(c["frames"]>0 for c in captures.values()),
        "no_frames":[key for key,value in captures.items() if not value["frames"]],
        "queues":data["queues"], "workers":data["workers"], "detectors":data["detectors"]}), flush=True)
