"""Offline model smoke test: no database, camera connections or webhooks."""
import time
import numpy as np
import torch
from ultralytics import YOLO

print("torch", torch.__version__, "cuda", torch.version.cuda, flush=True)
assert torch.cuda.device_count() == 2
torch.set_num_threads(1)
for device, path in [(0, "models/yolo26m.pt"), (1, "models/fire_smoke_yolov8.pt")]:
    model = YOLO(path)
    frames = [np.zeros((480, 640, 3), dtype=np.uint8) for _ in range(4)]
    for step in range(3):
        started = time.perf_counter()
        results = model.predict(frames, device=device, imgsz=640, verbose=False)
        torch.cuda.synchronize(device)
        assert len(results) == 4
        print(path, "gpu", device, "step", step, "batch_ms", round((time.perf_counter()-started)*1000), flush=True)
print("GPU_SMOKE_OK", flush=True)
