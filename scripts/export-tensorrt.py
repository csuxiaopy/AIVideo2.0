"""Build FP16 engines on the deployment GPU; never exports during service startup."""
import argparse
import hashlib
import json
import os
from pathlib import Path

os.environ["YOLO_AUTOINSTALL"] = "false"
parser = argparse.ArgumentParser()
parser.add_argument("model", type=Path)
parser.add_argument("--batch", type=int, default=2)
parser.add_argument("--device", default="0")
args = parser.parse_args()
import tensorrt
from ultralytics import YOLO

model = YOLO(str(args.model))
output = Path(model.export(format="engine", imgsz=640, half=True, dynamic=True,
                          batch=args.batch, workspace=1, device=args.device,
                          opset=17, simplify=False))
engine = args.model.with_suffix(".fp16.engine")
output.replace(engine)
meta = {"source_sha256": hashlib.sha256(args.model.read_bytes()).hexdigest(),
        "engine_sha256": hashlib.sha256(engine.read_bytes()).hexdigest(),
        "tensorrt": tensorrt.__version__, "imgsz": 640, "max_batch": args.batch,
        "precision": "fp16", "device": "Tesla T4"}
engine.with_suffix(".json").write_text(json.dumps(meta, indent=2))
print(engine)
