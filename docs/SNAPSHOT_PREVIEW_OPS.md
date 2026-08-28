# 周期抓帧与按需预览运维验收

## 运行机制

- 后台按照每路摄像头的 `frame_interval_seconds` 临时启动 FFmpeg，只抓取一帧后退出。
- 最新 JPEG 保存到 `/app/data/snapshots/{camera_id}.jpg`，由独立 Docker volume 持久化。
- 首页只读取最近快照，不建立 MJPEG 连接。
- 实时预览由 `POST /api/cameras/{id}/preview/start` 创建租约；关闭时调用 `preview/stop`。
- 浏览器每20秒发送心跳；MJPEG断开立即释放会话，超过60秒无心跳由后台清理。
- `MAX_LIVE_PREVIEWS` 默认4，按正在运行的摄像头预览进程计数。

## 分级压测

测试源必须能被App容器读取。下面示例复用快照卷中的测试JPEG，不连接额外摄像头：

```bash
python scripts/load_test.py \
  --base-url http://127.0.0.1:8100 \
  --source file:///app/data/snapshots/001.jpg \
  --cameras 8 \
  --frame-interval 60
```

依次将 `--cameras` 改为 `24`、`48`、`96`。每一级至少观察一个完整抽帧周期：

```bash
docker stats --no-stream yolo_vlm_monitor_app_1
docker top yolo_vlm_monitor_app_1 -eo pid,comm,args | grep ffmpeg
curl -sS http://127.0.0.1:8100/api/runtime/workers
```

验收要求：队列不连续增长、Worker失败数不增长、没有人工预览时持续FFmpeg数量为0；短时抓帧进程不超过普通Worker数。默认两个普通Worker、四路预览上限时，理论FFmpeg上限为6。

测试结束只清理由压测创建的摄像头：

```bash
for id in $(seq -f "%03g" 1 96); do
  curl -sS -X DELETE "http://127.0.0.1:8100/api/cameras/load-${id}" >/dev/null
done
```
