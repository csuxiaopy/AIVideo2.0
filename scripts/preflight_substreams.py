from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
from pathlib import Path


async def probe(row: dict[str, str], seconds: int, semaphore: asyncio.Semaphore) -> dict:
    camera_id = row["camera_id"].strip()
    source = row["substream_url"].strip()
    async with semaphore:
        started = time.perf_counter()
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
            "-timeout", "10000000", "-i", source, "-an", "-t", str(seconds),
            "-vf", "fps=1", "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=seconds + 15)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return {"camera_id": camera_id, "ok": False, "error": "probe timeout"}
        return {
            "camera_id": camera_id,
            "ok": process.returncode == 0,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "error": stderr.decode("utf-8", errors="replace")[-300:] if process.returncode else "",
        }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Validate camera_id,name,substream_url CSV")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=16)
    args = parser.parse_args()
    with args.csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"camera_id", "name", "substream_url"}
    if not rows or not required <= set(rows[0]):
        raise SystemExit("CSV columns must be camera_id,name,substream_url")
    ids = [row["camera_id"].strip() for row in rows]
    duplicates = sorted({camera_id for camera_id in ids if ids.count(camera_id) > 1})
    if duplicates:
        raise SystemExit(f"duplicate camera_id: {duplicates}")
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    results = await asyncio.gather(*(probe(row, args.seconds, semaphore) for row in rows))
    print(json.dumps({"total": len(results), "passed": sum(r["ok"] for r in results), "results": results}, ensure_ascii=False, indent=2))
    if not all(row["ok"] for row in results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
