# Fire/smoke pilot model

Place the reviewed pilot weight at `models/fire_smoke_yolov8.pt` before startup.

- Source: `mfranzon/fire-smoke-yolov8`
- File: `fire_smoke_yolov8.pt`
- SHA256: `ac0a10257b2bc1f20c9d957f8adeeb61dd6140322fc19d0b4a116cb491776d16`
- Classes: `fire`, `smoke`
- License: AGPL-3.0; internal pilot only

The service never downloads this file at startup and refuses to load a file whose configured SHA256 does not match. Before closed-source commercial production, replace it with an approved ONNX/TensorRT artifact and complete the Ultralytics and model license review.
