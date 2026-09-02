# 监衡：YOLO + 视觉大模型监控平台

独立的 96 路 RTSP 智能监控项目。每个摄像头可以多选黑屏、离岗、在岗记录、人流、玩手机、抽烟、烟火和闯入八种固定模式。系统不训练模型：人员、区域和跟踪使用公开 COCO 预训练 YOLO，烟火使用独立 YOLOv8 试点权重，玩手机和抽烟由外部视觉大模型联合检测。

## 检测模型

| 模型 | 权重/来源 | 用途 | 关键参数（默认值） |
| --- | --- | --- | --- |
| **通用 YOLO** | `models/yolo26s.pt`，COCO 公开预训练（AGPL-3.0） | person 检测，为离岗、在岗、人流和闯入提供候选与跟踪 | `YOLO_IMGSZ=640`、`YOLO_CONFIDENCE=0.35`、`YOLO_IOU=0.5`、CPU/GPU 由 `YOLO_DEVICE` 决定；在岗/离岗人员再叠加 `confidence≥0.45`，人员检测框只要与岗位 ROI 有任意交叠即视为在岗 |
| **烟火 YOLO** | `models/fire_smoke_yolov8.pt`（来源 `mfranzon/fire-smoke-yolov8`，试点用途） | fire / smoke 检测，独立于通用模型 | 火焰置信度 `fire_confidence=0.55`、烟雾 `smoke_confidence=0.45`；启动时校验 SHA256（`ac0a1025…76d16`），不匹配拒绝加载，绝不联网下载 |
| **视觉大模型（VLM）** | 外部 OpenAI 兼容接口（Base URL + API Key 在“系统配置”保存，密文存储） | 上班时间内每 3 分钟用同一单帧联合检测已启用的玩手机/抽烟行为 | 经济模型初筛；任一行为非 `none` 时，用同一帧和全部启用项统一交增强模型复核 |
| **统计黑屏检测** | 无模型，OpenCV 像素统计 | 黑屏判定 | 见下文告警逻辑 |

通用 YOLO 的多目标跟踪：每摄像头独立 ByteTrack（`supervision` 可用时），依赖缺失时回退到质心轻量跟踪器并在运行状态标记降级。

## 执行流程

1. **调度器**按每个摄像头自己的 `frame_interval_seconds` 周期触发抓帧，可选 1、5、10、20、30、60、120 秒；同周期摄像头在时间轴上均匀错峰，避免多路摄像头同时打满 CPU。1 秒频率适合少量重点摄像头，启用前应确认 FFmpeg 抓帧和 YOLO 推理吞吐充足。
2. 任务进入 **Redis 优先级队列**（Redis 不可用时降级为内存队列）：烟火 `critical` > 黑屏/闯入 `high` > 普通 `normal` > 纯人流 `low`；烟火走**独立队列**和独立 worker（`FIRE_SMOKE_WORKERS=1`），不会排队被普通任务阻塞。
3. worker 通过 FFmpeg 抓取单张 JPEG（短生命周期进程，抓完即退出），送入各检测模式。
4. 每种模式有独立的最小执行间隔（模式节流），如黑屏 `health_interval_seconds=5`、在岗/离岗记录 15 秒；玩手机与抽烟共用固定 180 秒联合行为节流。兼容字段 `behavior_interval_seconds` 仍保留，但不再控制这两种行为。
5. 检测结果写入 `analyses` 表；触发告警时保存带可视化标注的证据图到 `data/evidence/`（保留 `EVIDENCE_RETENTION_DAYS=30` 天），经事件总线推送 WebSocket，并按配置发送 Webhook。

## 警告判断逻辑

各模式的触发条件与告警行为（阈值均可在摄像头配置中调整）：

| 模式 | 判定逻辑 | 告警行为 |
| --- | --- | --- |
| **黑屏** | 灰度 `mean≤18` 且 `std≤12` 且近黑像素占比 `≥0.92` 视为异常帧，**连续 3 帧异常**才确认 | 确认后告警，severity=high |
| **离岗** | 在排班时段内（周排班 + 节假日 + 时区），置信度 ≥0.45 的人员检测框与岗位 ROI 没有任何交叠并持续达到 `off_duty_seconds`（默认 300 秒）时触发；检测框只要有一部分进入或接触 ROI 就视为在岗；班次开始有 `shift_grace_seconds`（默认 60 秒）宽限 | 确认后告警，severity=normal |
| **在岗记录** | 置信度 ≥0.45 的人员检测框只要与岗位 ROI 存在任意交叠即判定在岗，仅保存判定记录（每 15 秒一次） | **不产生告警** |
| **人流** | 共享 YOLO 检测与跟踪 ID，按脚点跨统计线的方向变化计“进入/离开”，同一 ID 当日只计一次，掉线 10 秒后清除跟踪状态 | 仅统计，**不产生告警** |
| **玩手机 / 抽烟** | 配置的上班时间内，每 3 分钟抽取当前单帧；一次 VLM 请求只判断已启用项。经济模型任一项非 `none` 时统一增强复核。须有明确操作/注视手机，或持烟、吸食动作、可关联烟雾证据；不使用 YOLO 候选或历史连续窗口 | 最终结果中每个 `confirmed` 行为独立告警，severity=normal；两项同时确认时共用原始证据帧 |
| **烟火** | 火焰：置信度 ≥0.55 且**连续 2 帧命中**确认；烟雾：置信度 ≥0.45 且**最近 5 帧中至少 3 帧命中**确认；未达时序条件记 `suspected`；检测器不可用记 `uncertain`（不能形成安全结论） | 确认后告警，severity=critical，冷却 60 秒 |
| **闯入** | 人员（`intrusion_confidence≥0.50`）跟踪脚点进入禁区多边形即触发，按跟踪 ID 去重，同一目标离开 1 秒后重置；告警按 `intrusion_cooldown_seconds`（默认 60 秒）限频 | 确认后告警，severity=high，**绕过全局告警冷却**，证据图叠加禁区框与人员框 |

**告警通用规则**：

- 同一摄像头同一模式的告警受 `alert_cooldown_seconds`（默认 300 秒）冷却约束（烟火、闯入另有专用冷却，见上表）。
- 告警会按已启用企业微信机器人的级别配置自动发送，也可在告警中心手动补发。
- 企业微信机器人依次接收 Markdown 告警摘要和证据图片，失败指数退避重试最多 5 次。

## 启动项目

完整的开发环境和生产环境启动说明见 [docs/STARTUP.md](docs/STARTUP.md)。

开发和生产环境均使用 PostgreSQL 且全部通过 Docker 启动。开发环境叠加 `compose.cpu.dev.yml`，一次启动前端、后端、PostgreSQL 和 Redis，并提供源码热更新；生产环境使用 `compose.cpu.yml` 启动完整服务。通用和烟火 `.pt` 权重均纳入 Git、随代码分发，运行时通过 Compose 只读挂载，不打入 Docker 镜像。

生产服务器的旧 Docker 兼容、数据备份和详细故障排查见 [docs/CPU_DOCKER_OPS_GUIDE.md](docs/CPU_DOCKER_OPS_GUIDE.md)。

## 配置流程

1. 在“视频源”添加 RTSP 地址并多选检测模式。
2. 新建时岗位 ROI 默认覆盖整幅画面，人流线默认位于画面中央。
3. 视频连接后进入“配置”，在实时截图上重新绘制岗位区域和统计线。
4. 设置工作日、上下班时间、离岗阈值和告警冷却。
5. 按摄像头选择抽帧频率：支持每 1、5、10、20、30、60、120 秒一帧；1 秒档会显著增加抓帧、YOLO 推理和数据库写入压力，建议仅用于少量重点视频并先压测。
6. 在“系统配置”保存经济模型、增强模型、Base URL 和 API Key，并先测试连接。
7. 在“企业微信机器人”中配置群机器人 URL 和自动发送级别，并先用测试或手动发送验证。

RTSP 密码中的 `@` 必须写成 `%40`。接口和日志只返回脱敏地址，密文使用 `APP_ENCRYPTION_KEY` 加密。

## 主要接口

- 摄像头：`/api/cameras`、`/modes`、`/geometry`、`/schedule`、`/analyze`、`/preview`、`/snapshot`
- 业务：`/api/dashboard`、`/api/alerts`、`/api/analyses`、`/api/traffic`
- 设置：`/api/settings/models`、`/api/settings/webhooks`（多目标管理与逐目标测试）
- 运行状态：`/health`、`/api/runtime/workers`、`/metrics`、`/ws/events`

FastAPI 自动接口文档位于 <http://127.0.0.1:8100/docs>。

## 企业微信机器人 Webhook

Webhook 是独立管理模块，可配置多个企业微信群机器人 HTTPS 地址。每个目标分别选择自动接收的
`normal`、`high`、`critical` 告警级别；告警中心也可批量选择历史告警和目标后手动发送。
投递结果按目标记录并展示成功数/总数，删除目标不会删除历史投递记录。

每条告警先发送企业微信 `markdown` 消息，再发送 Base64 + MD5 的 `image` 消息。证据图超过
2 MB 时会在内存中压缩，不改写原文件；两条消息都成功后投递才记为成功。HTTP 错误或企业微信
返回非零 `errcode` 均会触发指数退避重试，最多 5 次。

## Docker 运行验证

```powershell
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml ps
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml logs --tail=200 app web
Invoke-RestMethod http://127.0.0.1:8100/health | ConvertTo-Json -Depth 8
```

正式告警前应依次完成 10 路、32 路、64 路及最终 96 路影子运行，并根据现场样本统计召回率、告警准确率、不确定率、平均推理耗时和 P95。

## 生产注意事项

- 本系统第一版不带登录，只允许部署在隔离内网或受控反向代理之后。
- Ultralytics 开源版本涉及 AGPL-3.0；闭源商用前应完成许可证审查或采用适合的商业许可。
- GPU 上线前应将 YOLO26s 导出为 TensorRT FP16 `.engine`，并用现场 64 路 H.265 码流完成整链路压测。
- 安装完整 YOLO 依赖后使用每摄像头独立 ByteTrack；依赖不可用时仅以轻量跟踪器回退，并在运行状态中标记降级。上线前须用跨线样本核验方向和去重。
