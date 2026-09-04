from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time

import httpx


async def main() -> None:
    parser = argparse.ArgumentParser(description="Create simulated cameras and exercise dashboard APIs")
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--cameras", type=int, default=64)
    parser.add_argument("--video", default=os.getenv("VIDEO_PATH", ""))
    parser.add_argument("--source", default="", help="Direct rtsp:// or file:// source visible to the app")
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--frame-interval", type=int, choices=[1, 5, 10, 20, 30, 60, 120], default=60)
    args = parser.parse_args()
    if args.source:
        source = args.source
    elif args.video:
        source = f"file://{os.path.abspath(args.video).replace(os.sep, '/')}"
    else:
        raise SystemExit("Provide --source rtsp://... or --video C:/path/test.mp4")
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30) as client:
        for index in range(args.cameras):
            camera_id = f"load-{index + 1:03d}"
            response = await client.post("/api/cameras", json={
                "id": camera_id, "name": f"压测 {index + 1}", "rtsp_url": source,
                "modes": ["black_screen", "people_flow"],
                "geometry": {"post_roi": [], "flow_line": [[0.5, 0.05], [0.5, 0.95]]},
                "schedule": {"timezone": "Asia/Shanghai", "weekly": {}, "holidays": []},
                "frame_interval_seconds": args.frame_interval,
            })
            if response.status_code not in {201, 409}:
                print(camera_id, response.status_code, response.text)
        latencies = []
        for _ in range(args.rounds):
            started = time.perf_counter()
            response = await client.get("/api/dashboard")
            response.raise_for_status()
            latencies.append((time.perf_counter() - started) * 1000)
            await asyncio.sleep(1)
        runtime = (await client.get("/api/runtime/workers")).json()
        print({
            "cameras": args.cameras,
            "frame_interval_seconds": args.frame_interval,
            "rounds": args.rounds,
            "dashboard_p50_ms": round(statistics.median(latencies), 2),
            "dashboard_p95_ms": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 2),
            "active_previews": runtime.get("media", {}).get("active_previews"),
            "registered_cameras": runtime.get("media", {}).get("registered_cameras"),
            "queues": runtime.get("queues", {}),
        })


if __name__ == "__main__":
    asyncio.run(main())
