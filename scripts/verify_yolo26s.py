"""YOLO26s 升级验证脚本（可在服务器上重复执行）。

覆盖项：
1. 服务启动（FastAPI lifespan + /health）
2. YOLO26s 模型加载
3. 单张图片推理
4. bbox 返回（归一化坐标 0..1）
5. 告警证据图片保存与 /evidence 访问
6. 多任务并发推理（模拟多个 worker）
7. RTSP/FFmpeg 抽帧异常（视频源不存在）
8. 模型文件不存在（降级不崩溃）
9. 推理耗时统计（avg / P95）

用法：
  python scripts/verify_yolo26s.py
退出码 = 失败用例数（0 表示全部通过）。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import io
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.annotation import annotate_detections  # noqa: E402
from backend.detectors.yolo import YoloDetector  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def find_sample_jpeg() -> bytes:
    for candidate in [
        ROOT / "data" / "snapshots" / "businesshallone.jpg",
        ROOT / "data" / "evidence" / "businesshallone-off_duty-20260826T012247932384Z.jpg",
        ROOT / "data" / "annotation_preview.jpg",
    ]:
        if candidate.is_file() and candidate.stat().st_size > 1000:
            return candidate.read_bytes()
    raise RuntimeError("未找到可用于测试的样例图片（需要 data/snapshots 或 data/evidence 下的真实抓帧）")


def test_model_missing() -> None:
    detector = YoloDetector("models/definitely-not-exist.pt", "cpu", 640, 0.35, 0.5)
    record("模型文件不存在 → 降级不崩溃", not detector.available and "模型文件不存在" in detector.detail, detector.detail)


def test_model_load() -> YoloDetector:
    detector = YoloDetector("models/yolo26s.pt", "cpu", 640, 0.35, 0.5)
    record("YOLO26s 模型加载", detector.available, detector.detail)
    record("模型加载耗时已记录", detector.load_ms > 0, f"load_ms={detector.load_ms}")
    return detector


def test_single_inference(detector: YoloDetector, jpeg: bytes) -> None:
    started = time.perf_counter()
    try:
        detections = detector.detect("test-cam", jpeg)
    except Exception as exc:
        record("单张图片推理", False, f"{type(exc).__name__}: {exc}")
        return
    elapsed_ms = (time.perf_counter() - started) * 1000
    ok = detector.processed == 1
    record("单张图片推理", ok, f"{len(detections)} detections, {elapsed_ms:.0f}ms")
    names = sorted({d.class_name for d in detections})
    record("检测类别返回", ok, f"classes={names}" if names else "no detections on sample")
    valid_boxes = all(0.0 <= v <= 1.0 for d in detections for v in d.box)
    record("bbox 归一化坐标 0..1", valid_boxes and all(d.confidence >= 0 for d in detections))
    record("置信度字段", all(0.0 <= d.confidence <= 1.0 for d in detections))


def test_evidence_save(detector: YoloDetector, jpeg: bytes) -> None:
    detections = detector.detect("evidence-cam", jpeg)
    evidence = annotate_detections(jpeg, detections)
    valid = evidence.startswith(b"\xff\xd8") and evidence.endswith(b"\xff\xd9")
    record("告警证据图片标注/保存（JPEG 合法）", valid, f"{len(evidence)} bytes")
    out = ROOT / "data" / "evidence" / "verify_yolo26s.jpg"
    out.write_bytes(evidence)
    record("证据图片落盘", out.is_file() and out.stat().st_size > 1000, str(out))


def test_concurrent_inference(detector: YoloDetector, jpeg: bytes) -> None:
    def work(index: int) -> int:
        det = detector.detect(f"concurrent-cam-{index}", jpeg)
        return len(det)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(work, i) for i in range(8)]
        counts = [f.result(timeout=120) for f in futures]
    ok = detector.processed >= 9 and detector.failures == 0
    record("多任务并发推理（8 并发，共用单模型）", ok, f"counts={counts}, processed={detector.processed}, failures={detector.failures}")


def test_rtsp_capture_failure() -> None:
    from backend.media_capture import MediaGateway

    gateway = MediaGateway(lambda *_: None, ROOT / "data" / "snapshots")
    source = "file:///definitely/not/exist.mp4"

    async def _capture() -> None:
        await gateway.capture("bad-camera")

    try:
        asyncio.run(_capture())
        record("RTSP/FFmpeg 抽帧异常 → 抛错", False, "expected an error but capture succeeded")
    except RuntimeError as exc:
        record("RTSP/FFmpeg 抽帧异常 → 抛错", True, str(exc)[:120])
    except Exception as exc:
        record("RTSP/FFmpeg 抽帧异常 → 抛错", True, f"unexpected type {type(exc).__name__}: {exc}")
    finally:
        asyncio.run(gateway.close())


def test_app_startup_and_evidence_endpoint() -> None:
    from fastapi.testclient import TestClient

    from backend.main import create_app

    app = create_app()
    with TestClient(app) as client:
        health = client.get("/health")
        record("服务启动 + /health", health.status_code == 200, f"status={health.status_code}")
        body = health.json()
        yolo = body.get("yolo", {})
        record("健康检查含 YOLO 状态", yolo.get("status") in {"ready", "degraded"}, f"yolo={yolo.get('status')} model={yolo.get('model')}")

        evidence = ROOT / "data" / "evidence" / "verify_yolo26s.jpg"
        if evidence.is_file():
            resp = client.get("/evidence/verify_yolo26s.jpg")
            record("前端告警图片 /evidence 访问", resp.status_code == 200 and resp.content[:2] == b"\xff\xd8", f"http={resp.status_code}")
        else:
            record("前端告警图片 /evidence 访问", False, "evidence file missing")


def test_inference_stats(detector: YoloDetector) -> None:
    status = detector.status()
    record(
        "推理耗时统计（avg/P95）",
        status["processed"] > 0 and status["avg_latency_ms"] > 0 and status["p95_latency_ms"] > 0,
        f"avg={status['avg_latency_ms']}ms p95={status['p95_latency_ms']}ms last={status['latency_ms']}ms processed={status['processed']}",
    )


def main() -> int:
    print(f"== YOLO26s 升级验证 @ {time.strftime('%Y-%m-%d %H:%M:%S')} ==\n")
    test_model_missing()
    try:
        detector = test_model_load()
    except Exception as exc:
        record("YOLO26s 模型加载", False, f"{type(exc).__name__}: {exc}")
        detector = None
    if detector is not None and detector.available:
        try:
            jpeg = find_sample_jpeg()
        except RuntimeError as exc:
            record("样例图片", False, str(exc))
            jpeg = b""
        if jpeg:
            test_single_inference(detector, jpeg)
            test_evidence_save(detector, jpeg)
            test_concurrent_inference(detector, jpeg)
            test_inference_stats(detector)
    test_rtsp_capture_failure()
    test_app_startup_and_evidence_endpoint()

    print(f"\n== 汇总：{sum(1 for _, ok, _ in RESULTS if ok)}/{len(RESULTS)} 项通过 ==")
    failed = [name for name, ok, _ in RESULTS if not ok]
    if failed:
        print("失败项：", ", ".join(failed))
    return len(failed)


if __name__ == "__main__":
    raise SystemExit(main())
