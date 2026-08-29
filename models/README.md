# Detection models

## General YOLO26s

- File: `yolo26s.pt`
- Source: official Ultralytics assets release `v8.4.0`
- Download URL: `https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo26s.pt`
- Size: `20,422,725` bytes
- SHA256: `646f8bc3fe0a656803d95c294f7852321748cb29d13466a1af8862e2db384a1b`
- Classes: COCO detection classes; the current business logic primarily uses `person` and `cell phone`.

The weight is committed to Git with the project. Deployment hosts receive it through `git clone` or `git pull`. Docker still excludes weights from the image build context and Compose mounts the repository's `models/` directory read-only at `/app/models`.

## Fire/smoke pilot model

This reviewed pilot weight is committed to Git at `models/fire_smoke_yolov8.pt`.

- Source: `mfranzon/fire-smoke-yolov8`
- File: `fire_smoke_yolov8.pt`
- SHA256: `ac0a10257b2bc1f20c9d957f8adeeb61dd6140322fc19d0b4a116cb491776d16`
- Classes: `fire`, `smoke`
- License: AGPL-3.0; internal pilot only

The service never downloads this file at startup and refuses to load a file whose configured SHA256 does not match. Deployment hosts receive it through Git. Before closed-source commercial production, replace it with an approved ONNX/TensorRT artifact and complete the Ultralytics and model license review.
