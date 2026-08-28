# AIVideo2.0

真正的AI视频巡检

# 监衡：YOLO + 视觉大模型监控平台

独立的 96 路 RTSP 智能监控项目。每个摄像头可以多选黑屏、离岗、在岗记录、人流、玩手机、抽烟、烟火和闯入八种固定模式。系统不训练模型：人员、手机、区域和跟踪使用公开 COCO 预训练 YOLO，烟火使用独立 YOLOv8 试点权重，玩手机和抽烟由外部视觉大模型分级复核。

## 检测模型

| 模型 | 权重/来源 | 用途 | 关键参数（默认值） |
| --- | --- | --- | --- |
| **通用 YOLO** | `models/yolo26s.pt`，COCO 公开预训练（AGPL-3.0） | person / cell phone 检测，为离岗、在岗、人流、玩手机、抽烟、闯入提供候选与跟踪 | `YOLO_IMGSZ=640`、`YOLO_CONFIDENCE=0.35`、`YOLO_IOU=0.5`、CPU/GPU 由 `YOLO_DEVICE` 决定；人员判断再叠加 `confidence≥0.45`（脚点判定）。切换模型（yolo26n/s/m、自训练 best.pt）只改 `YOLO_MODEL` 或系统配置，无需改代码 |
| **烟火 YOLO** | `models/fire_smoke_yolov8.pt`（来源 `mfranzon/fire-smoke-yolov8`，试点用途） | fire / smoke 检测，独立于通用模型 | 火焰置信度 `fire_confidence=0.55`、烟雾 `smoke_confidence=0.45`；启动时校验 SHA256（`ac0a1025…76d16`），不匹配拒绝加载，绝不联网下载 |
| **视觉大模型（VLM）** | 外部 OpenAI 兼容接口（Base URL + API Key 在“系统配置”保存，密文存储） | 玩手机/抽烟的分级复核 | 经济模型（初筛）→ 非结论再交增强模型（确认） |
| **统计黑屏检测** | 无模型，OpenCV 像素统计 | 黑屏判定 | 见下文告警逻辑 |

通用 YOLO 的多目标跟踪：每摄像头独立 ByteTrack（`supervision` 可用时），依赖缺失时回退到质心轻量跟踪器并在运行状态标记降级。

## 执行流程

1. **调度器**按每个摄像头自己的 `frame_interval_seconds` 周期触发抓帧；同周期摄像头在时间轴上均匀错峰，避免 96 路同时打满 CPU。
2. 任务进入 **Redis 优先级队列**（Redis 不可用时降级为内存队列）：烟火 `critical` > 黑屏/闯入 `high` > 普通 `normal` > 纯人流 `low`；烟火走**独立队列**和独立 worker（`FIRE_SMOKE_WORKERS=1`），不会排队被普通任务阻塞。
3. worker 通过 FFmpeg 抓取单张 JPEG（短生命周期进程，抓完即退出），送入各检测模式。
4. 每种模式有独立的最小执行间隔（模式节流），如黑屏 `health_interval_seconds=5`、行为复核 `behavior_interval_seconds=15`、在岗/离岗记录 15 秒，避免重复分析。
5. 检测结果写入 `analyses` 表；触发告警时保存带可视化标注的证据图到 `data/evidence/`（保留 `EVIDENCE_RETENTION_DAYS=30` 天），经事件总线推送 WebSocket，并按配置发送 Webhook。

## 警告判断逻辑

各模式的触发条件与告警行为（阈值均可在摄像头配置中调整）：

| 模式 | 判定逻辑 | 告警行为 |
| --- | --- | --- |
| **黑屏** | 灰度 `mean≤18` 且 `std≤12` 且近黑像素占比 `≥0.92` 视为异常帧，**连续 3 帧异常**才确认 | 确认后告警，severity=high |
| **离岗** | 在排班时段内（周排班 + 节假日 + 时区），岗位 ROI 内持续无人员（按脚点判定）；持续时长达到 `off_duty_seconds`（默认 300 秒）触发；班次开始有 `shift_grace_seconds`（默认 60 秒）宽限 | 确认后告警，severity=normal |
| **在岗记录** | 岗位 ROI 内是否检测到人员，仅保存判定记录（每 15 秒一次） | **不产生告警** |
| **人流** | 共享 YOLO 检测与跟踪 ID，按脚点跨统计线的方向变化计“进入/离开”，同一 ID 当日只计一次，掉线 10 秒后清除跟踪状态 | 仅统计，**不产生告警** |
| **玩手机** | 岗位 ROI 内有人且附近存在 cell phone 候选 → 经济模型初筛，`none` 直接结束，否则增强模型确认（须“明确操作或注视手机”，仅看到手机不确认）；**60 秒内至少 2 个确认窗口**才告警 | 确认后告警，severity=normal |
| **抽烟** | 画面有人即采样最近 8 帧送 VLM 分级复核（须“持烟、吸食动作或可关联烟雾证据”；喝水、吃东西、打电话明确排除）；同样执行 **60 秒内 ≥2 个确认窗口**规则 | 确认后告警，severity=normal |
| **烟火** | 火焰：置信度 ≥0.55 且**连续 2 帧命中**确认；烟雾：置信度 ≥0.45 且**最近 5 帧中至少 3 帧命中**确认；未达时序条件记 `suspected`；检测器不可用记 `uncertain`（不能形成安全结论） | 确认后告警，severity=critical，冷却 60 秒 |
| **闯入** | 人员（`intrusion_confidence≥0.50`）跟踪脚点进入禁区多边形即触发，按跟踪 ID 去重，同一目标离开 1 秒后重置；告警按 `intrusion_cooldown_seconds`（默认 60 秒）限频 | 确认后告警，severity=high，**绕过全局告警冷却**，证据图叠加禁区框与人员框 |

**告警通用规则**：

- 同一摄像头同一模式的告警受 `alert_cooldown_seconds`（默认 300 秒）冷却约束（烟火、闯入另有专用冷却，见上表）。
- 默认 `SHADOW_MODE=true`：页面正常记录和展示告警，但**不发送 Webhook**；改为 `false` 才真正外发。
- Webhook 请求带 `X-Monitor-Timestamp` 与 `X-Monitor-Signature`（HMAC-SHA256 签名），失败指数退避重试最多 5 次。

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
