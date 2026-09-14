# 双 T4 上线手册

## 前置条件

- 备份 `.env`、数据库、当前镜像 ID 和 Compose 配置。
- NVIDIA 驱动需支持 CUDA 12.1，且 `nvidia-container-toolkit` 已配置给 Docker。
- `nvidia-smi` 和 `docker run --rm --gpus all nvidia/cuda:12.1.1-base-ubuntu22.04 nvidia-smi` 均能看到两张 T4。
- 子码流清单必须是 UTF-8 CSV，列固定为 `camera_id,name,substream_url`。

## 子码流预检

```bash
python scripts/preflight_substreams.py cameras.csv --seconds 30 --concurrency 16
```

所有在线摄像头通过后，在管理页批量导入清单。已知离线的 `172.16.230.146:554` 应单独标记，不纳入性能验收。

## 构建与切换

```bash
docker compose -f compose.cpu.yml -f compose.gpu.yml build app
docker compose -f compose.cpu.yml -f compose.gpu.yml run --rm app python -c "import torch; print(torch.cuda.is_available(), torch.cuda.device_count()); assert torch.cuda.device_count() == 2"
docker compose -f compose.cpu.yml -f compose.gpu.yml up -d
docker compose -f compose.cpu.yml -f compose.gpu.yml exec app alembic upgrade head
```

上线后观察 `/api/runtime/workers` 和 Prometheus 指标至少30分钟。在线流的 `monitor_capture_fps` 应不低于0.95，`monitor_latest_frame_age_seconds` P95不高于2秒，两个任务队列不得持续增长。

## 回滚

```bash
docker compose -f compose.cpu.yml -f compose.gpu.yml down
docker compose -f compose.cpu.yml up -d
```

新增数据库字段可保留；CPU版本会继续使用主码流或已配置子码流，不需要降级数据库。
