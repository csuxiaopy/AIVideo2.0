# 监衡：YOLO + 视觉大模型监控平台

独立的 64 路 RTSP 智能监控项目。每个摄像头可以多选黑屏、离岗、在岗记录、人流、玩手机和抽烟六种固定模式。系统不训练模型：人员、手机、区域和跟踪使用公开 COCO 预训练 YOLO，玩手机和抽烟由外部视觉大模型分级复核。

## 能力边界

- 黑屏：本地亮度、方差和近黑像素比例，连续 3 次异常才告警。
- 离岗：岗位 ROI + 周排班 + 持续无人阈值；只产生离岗告警。
- 在岗：保存判定记录，不生成告警。
- 人流：共享 YOLO 检测结果，按跟踪 ID 进行跨线进入/离开统计。
- 玩手机：person/cell phone 候选进入经济模型，非 `none` 再由增强模型确认。
- 抽烟：存在人员时采集连续帧进行双模型复核，不使用错误的邻近类别替代。
- 玩手机/抽烟只有增强模型确认且 60 秒内至少 2 个窗口命中才告警。
- 默认 `SHADOW_MODE=true`：页面记录告警，但不发送 Webhook。

## 本地开发

需要 Python 3.11+、Node.js 20+ 和 FFmpeg。后端不安装 YOLO 时仍可运行管理页面，但检测器显示“降级”；安装 `requirements-yolo.txt` 后会加载公开预训练权重。

```powershell
cd "D:\project\AI video\yolo_vlm_monitor"
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

cd frontend
npm install
npm run build
cd ..

.\.venv\Scripts\python.exe run.py
```

打开 <http://127.0.0.1:8100>。

安装 CPU YOLO：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-yolo.txt
```

`.env` 开发默认使用 SQLite；生产 Compose 自动切换 PostgreSQL 和 Redis。生产前必须修改 `APP_ENCRYPTION_KEY`，修改后既有 API Key 和 RTSP 密码将无法解密。

## Docker

CPU 开发环境：

```powershell
$env:APP_ENCRYPTION_KEY = "请替换为至少32位的随机值"
docker compose -f compose.cpu.yml up --build
```

GPU 环境需要 NVIDIA 驱动与 Container Toolkit：

```powershell
$env:APP_ENCRYPTION_KEY = "请替换为至少32位的随机值"
docker compose -f compose.cpu.yml -f compose.gpu.yml up --build
```

同时启动 Prometheus 和 Grafana：

```powershell
docker compose -f compose.cpu.yml --profile monitoring up --build
```

- 应用：<http://127.0.0.1:8100>
- Prometheus：<http://127.0.0.1:9090>
- Grafana：<http://127.0.0.1:3000>

## 配置流程

1. 在“视频源”添加 RTSP 地址并多选检测模式。
2. 新建时岗位 ROI 默认覆盖整幅画面，人流线默认位于画面中央。
3. 视频连接后进入“配置”，在实时截图上重新绘制岗位区域和统计线。
4. 设置工作日、上下班时间、离岗阈值和告警冷却。
5. 在“系统配置”保存经济模型、增强模型、Base URL 和 API Key，并先测试连接。
6. 影子运行验证完成后，将 `.env` 的 `SHADOW_MODE` 改为 `false` 才会发送 Webhook。

RTSP 密码中的 `@` 必须写成 `%40`。接口和日志只返回脱敏地址，密文使用 `APP_ENCRYPTION_KEY` 加密。

## 主要接口

- 摄像头：`/api/cameras`、`/modes`、`/geometry`、`/schedule`、`/analyze`、`/preview`、`/snapshot`
- 业务：`/api/dashboard`、`/api/alerts`、`/api/analyses`、`/api/traffic`
- 设置：`/api/settings/models`、`/api/settings/webhook`
- 运行状态：`/health`、`/api/runtime/workers`、`/metrics`、`/ws/events`

FastAPI 自动接口文档位于 <http://127.0.0.1:8100/docs>。

## Webhook

请求包含：

- `X-Monitor-Timestamp`
- `X-Monitor-Signature: sha256=<hex>`

签名原文为 `<timestamp>.<按键排序且无空格的JSON>`，使用配置密钥计算 HMAC-SHA256。失败采用指数退避，最多 5 次。

## 测试与 64 路验证

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts\load_test.py --video D:\videos\test.mp4 --cameras 64
```

压测脚本用同一测试视频模拟多路输入，用于验证共享拉流、调度和 API 稳定性，不代表现场识别准确率。正式告警前应依次完成 10 路、32 路、64 路影子运行，并根据现场样本统计召回率、告警准确率和不确定率。

## 生产注意事项

- 本系统第一版不带登录，只允许部署在隔离内网或受控反向代理之后。
- Ultralytics 开源版本涉及 AGPL-3.0；闭源商用前应完成许可证审查或采用适合的商业许可。
- GPU 上线前应将 YOLO26s 导出为 TensorRT FP16 `.engine`，并用现场 64 路 H.265 码流完成整链路压测。
- 安装完整 YOLO 依赖后使用每摄像头独立 ByteTrack；依赖不可用时仅以轻量跟踪器回退，并在运行状态中标记降级。上线前须用跨线样本核验方向和去重。
