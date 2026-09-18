# 江苏有线无锡AI营业厅分析：YOLO + 视觉大模型视频监控平台

江苏有线无锡AI营业厅分析是一套面向营业厅、工位、出入口和库房的视频智能分析平台。系统通过 FFmpeg 周期抽帧，结合本地 YOLO、像素统计、目标跟踪和外部视觉大模型，实现黑屏、离岗、在岗、人流、玩手机、吸烟、烟火和区域入侵检测，并提供告警留证、企业微信推送、人流月报、账号权限、审计日志和运行状态监控。

项目品牌图标统一存放在 `frontend/public/brand-icon.png`，同时用于浏览器页签图标、登录页和系统侧栏。替换品牌图标时保持 PNG 方形画布，并沿用该文件名即可。

项目以 96 路 RTSP 接入为设计目标。当前生产方案使用双 GPU 分担通用 YOLO，其中 GPU 1 还承担低频烟火检测，并以 1 FPS 采集为目标；实际可承载路数和有效分析频率仍取决于视频编码、在线路数、组批效率、CPU/GPU、模型大小和外部 VLM 延迟，正式上线前必须使用现场码流持续压测。

## 核心能力

- 支持 RTSP、RTSPS、RTMP、HTTP、HTTPS 和 `file://` 视频源。
- 每个摄像头可独立组合八类检测模式。
- 内置员工工位、客户区/入口、库房/安全区场景模板。
- 支持每 1、5、10、20、30、60、120 秒抓取一帧。
- 可在实时预览上绘制岗位 ROI 和入侵禁区；人流统计无需配置几何图形。
- 烟火任务使用独立队列和 worker，避免被普通任务阻塞。
- 保存带标注的告警证据，支持筛选、清理和手动补发。
- 支持多个企业微信机器人、分级推送和失败重试。
- 支持摄像头目录管理、批量移动和按目录聚合业务数据。
- 提供人流趋势、当前人数 Top 5、营业厅人流排名、月度统计和 Excel 导出。
- 提供管理员/普通用户权限、图形验证码、审计日志、健康检查和 Prometheus 指标。

## 功能模块

### 1. 账号与权限

系统不开放注册。首次启动且数据库中没有账号时，根据以下环境变量创建首个管理员：

- `ADMIN_USERNAME`：管理员登录名。
- `ADMIN_DISPLAY_NAME`：管理员显示名称。
- `ADMIN_PASSWORD`：初始密码，要求 8–128 位。

管理员可以创建普通用户、修改显示名称、启停账号、重置密码和删除账号。普通用户可以访问监控总览、人流报表和告警中心，但不能管理摄像头、模型、Webhook、保留策略或账号。

登录时必须填写服务端生成的图形验证码。验证码为一次性凭据，校验失败或过期后需要刷新再试。

会话连续闲置 8 小时后默认失效。修改或重置密码、停用或删除账号时，该账号已有会话会失效。HTTPS 部署应设置 `SECURE_COOKIES=true`，并使用 `ALLOWED_ORIGINS` 限制 WebSocket 来源。

### 2. 监控总览

总览页集中展示摄像头总数与在线状态、今日告警、烟火紧急告警、最近快照、抽帧周期、启用模式、最近告警和 worker 状态。当前画面人数可在系统配置中隐藏。

监控矩阵默认显示后台周期快照，不会为所有摄像头自动开启连续视频流，从而减少连接与解码开销。

### 3. 视频源管理

管理员可以单个或批量添加视频源，并配置：

- 业务 ID、显示名称、视频地址和启用状态。
- 所属目录；目录支持新建、重命名和删除，删除目录后其中摄像头自动移入“未分组”。
- 摄像头列表支持勾选、全选、批量移动目录、批量设置离岗排班和批量删除；批量删除会统一释放对应运行状态。
- 场景类型与检测模式组合。
- 1、5、10、20、30、60、120 秒抽帧周期。
- 岗位区域和入侵禁区；人流统计无需绘制区域。
- 周排班、多个班次、时区和节假日。
- 离岗时长、班次宽限、告警冷却及检测阈值。

编辑时已保存的视频地址不会回显；留空表示保持原地址，填写新地址才会替换。API 和日志只返回脱敏地址，凭据使用 `APP_ENCRYPTION_KEY` 加密存储。RTSP 密码中的 `@` 应编码为 `%40`。

实时预览采用按需租约：仅打开预览时建立连续取流。预览数量、帧率和超时分别由 `MAX_LIVE_PREVIEWS`、`LIVE_PREVIEW_FPS`、`LIVE_PREVIEW_TIMEOUT_SECONDS` 控制。

### 4. 场景模板与空间配置

| 场景模板 | 默认用途 | 默认模式 | 必需图形 |
| --- | --- | --- | --- |
| 员工工位 | 行为规范和离岗管理 | 离岗、玩手机、黑屏 | 岗位 ROI |
| 客户区/入口 | 营业厅客流统计 | 人流、黑屏 | 无需几何配置 |
| 库房/安全区 | 全天安全检测 | 烟火、区域入侵、黑屏 | 入侵禁区 |

岗位 ROI、人流 ROI 和禁区至少需要 3 个归一化坐标点；人流 ROI 默认为全屏。烟火、区域入侵和黑屏属于全天安全模式，不受普通排班限制。

### 5. 智能检测

| 模式 | 实现与判定 | 结果 |
| --- | --- | --- |
| 黑屏 | OpenCV 灰度统计；默认 `mean≤18`、`std≤12`、近黑比例 `≥0.92`，连续 3 帧异常后确认 | `high` 告警 |
| 离岗 | 排班内检测置信度不低于 0.30 的人员框是否与岗位 ROI 相交；本地连续无人独立计时，达到当前时段阈值后使用首次跨过阈值的固定帧进行 VLM 终审 | 本地阈值与 VLM 均确认后产生 `normal` 告警；有效的联合请求结果可复用 |
| 在岗记录 | 人员框与岗位 ROI 有任意交叠即判定在岗 | 仅记录 |
| 人流 | YOLO 人员检测和独立跟踪，每个 Track 首次出现在人流 ROI 内时累计一次 | 仅统计 |
| 玩手机 | 外部 VLM 按每路频率分析请求创建时的固定帧，连续判定和证据时间均使用该帧的采样时间 | 默认连续 10 分钟确认后产生 `normal` 告警 |
| 吸烟 | 外部 VLM 分析持烟、吸食动作或可关联烟雾 | `normal` 告警 |
| 烟火 | 独立 YOLO，火焰与烟雾默认阈值均为 0.90；火焰连续 2 帧命中，烟雾最近 5 帧至少 3 帧命中 | `critical` 告警 |
| 区域入侵 | 人员跟踪脚点进入禁区即触发，按跟踪 ID 去重 | `high` 告警 |

玩手机和吸烟共用一次单模型联合 VLM 请求。任务创建时会把 JPEG、`sampled_at` 和事件 ID 封装为不可变载荷，后续排队、重试和告警留证都复用该帧，不会改取回调时的最新画面。当同一路存在未中断的本地无人事件时，行为请求会顺带判断离岗；该确认与无人事件绑定，人员返回、排班结束或配置变化后立即失效。

离岗时长由本地规则按排班时段独立计时，每个时段可以设置独立阈值；默认 09:00–11:00 为 5 分钟、12:00–13:30 为 15 分钟、13:30–17:00 为 5 分钟。首次跨过阈值时固定保存阈值帧：若当前无人事件已有有效的 VLM 离岗确认则不重复调用，否则始终使用该阈值帧复核，失败重试也不更换证据。离岗告警 `START` 为无人开始时间，`END` 固定为 `START + 有效阈值`，模型排队和落库延迟只影响创建时间。

告警按目录聚合：目录内各摄像头独立完成本地阈值和 VLM 确认，全部满足后生成一条目录告警并保存各自的阈值证据帧，目录事件区间由最后满足条件的成员确定。未确认、离线、未排班、模型未配置或调用失败都会阻止整组告警。持续全员离岗时，每路重新取得确认图片并超过目录最大冷却时间后可再次告警。

所有正式告警使用“目录名+营业厅视频”作为名称，未分组摄像头统一归入“未分组营业厅视频”。玩手机保留每路持续判定时间，目录内任一路达到阈值即可告警，并按目录最大冷却时间合并；其他告警仍逐摄像头触发，具体监控源写入告警原因。

通用 YOLO 使用 COCO 预训练模型识别人。安装完整跟踪依赖时，每个摄像头使用独立 ByteTrack；依赖不可用时回退到轻量质心跟踪器，并在运行状态中标记降级。

### 6. 告警中心

告警中心支持按检测模式和严重级别组合筛选，可查看目录告警名称、触发时间、置信度、判定原因和单张或多张证据图片。用户可以多选历史告警发送到指定机器人；多图告警会向企业微信依次发送全部证据。管理员还可按保留天数清理告警记录及全部证据。

玩手机和离岗使用目录成员中最大的 `alert_cooldown_seconds` 限频；其他普通模式仍使用摄像头级冷却，烟火和区域入侵继续使用各自的专用规则。

### 7. 人流报表

人流模块基于人员跟踪 ID 统计进入 ROI 的人次，展示今日总人流、当前画面人数、当日趋势、当前画面人数 Top 5、按目录汇总的今日人流排名以及逐摄像头统计。趋势图与两类排名在宽屏下并排展示，并随实时事件自动更新。

人流统计直接复用通用 YOLO 的 Person 检测和 ByteTrack 跟踪结果，不会额外运行一套模型。人员框中心点首次出现在人流 ROI 内时立即累计一次，无需先在 ROI 外出现，也不等待稳定帧确认。

同一 Track 生命周期只累计一次，离开 ROI 后再次进入不会重复累计。Track 连续丢失 3 个检测周期后状态删除；之后产生的新 Track ID 首次出现在 ROI 内时会作为新的人流重新统计。

摄像头高级选项保留原有人流配置字段以兼容既有配置；稳定确认帧数和边缘比例不再参与计数。Debug 开启后，实时预览显示 Track ID、ROI 内外状态、跟踪周期、计数事件、当前画面人数和今日累计人流。

系统配置可以隐藏人流报表菜单或总览中的当前人数。隐藏只影响界面展示，不会停止后台统计或删除历史数据。

月度人流统计按 `Asia/Shanghai` 自然日和当前摄像头目录汇总，可选择当前或历史月份查看每个营业厅的每日人流、月合计、每日总计和全月总计。未来日期显示为空，未来月份不允许查询；报表可导出为带冻结表头和筛选器的 `.xlsx` 文件。

### 8. 企业微信机器人

管理员可维护多个企业微信群机器人，每个目标可独立启停，并选择自动接收的 `normal`、`high`、`critical` 级别。

投递时先发送 Markdown 摘要，再发送 Base64 + MD5 格式的证据图片。图片超过 2 MB 时在内存中压缩，不改写原证据文件。HTTP 错误或企业微信返回非零 `errcode` 时指数退避，最多尝试 5 次；两条消息都成功才计为投递成功。

### 9. 日志管理

管理员可以分页查看用户操作、摄像头分析和外部大模型调用三类日志，按时间及各类别字段筛选、查看详情并将当前筛选结果导出为 UTF-8 CSV。日志中记录操作结果、分析状态、模型延迟、Token 用量和错误信息等排障信息；敏感配置不会作为明文日志输出。

三类日志使用统一保留天数，默认保留 30 天，由系统配置中的 `log_retention_days` 控制并随自动清理任务删除。

### 10. 系统配置

- 界面展示：控制人流报表和当前人数是否显示。
- 数据保留：分别设置告警与日志保留天数，并控制到期自动清理。
- 外部视觉模型：配置 OpenAI 兼容 Base URL、API Key 和检测模型，并测试连接。
- 本地检测器：配置通用 YOLO、烟火模型和运行设备，查看加载状态。
- 能力注册表：区分已就绪、实验性和规划能力；规划能力仅展示，不进入检测任务。

## 系统架构与执行流程

```text
RTSP / 视频文件
       │
       ▼
周期调度器 ── FFmpeg 单帧抓取 ── 快照缓存
       │
       ├── critical：烟火独立队列 ── 烟火 worker ── 烟火 YOLO
       │
       └── high / normal / low：普通队列 ── 分析 worker
                                             ├── 黑屏统计
                                             ├── 双 GPU 通用 YOLO + 跟踪
                                             └── 持久化复核任务 ── VLM worker
                                                      │
                                                      ▼
                         PostgreSQL ← 后台任务 / 分析记录 / 告警 / 配置
                                                      │
                             证据图片 + WebSocket + 企业微信
```

1. 调度器按每路摄像头的 `frame_interval_seconds` 触发任务，同周期摄像头错峰执行。
2. 任务进入 Redis 优先级队列；Redis 不可用时降级为进程内队列。
3. 烟火使用独立队列和 worker，其余任务按安全性和业务类型确定优先级。
4. 生产环境将摄像头稳定分配到 GPU 0/1 的通用 YOLO，GPU 1 同时处理低频烟火任务；YOLO 支持小批量组批并限制算子线程数。
5. FFmpeg 常驻分析流按目标 FPS 输出 JPEG；生产环境优先使用双 GPU NVDEC，超出硬解分配或显式指定的摄像头使用 CPU 解码。
6. VLM 复核、分析写入、客流写入、告警和通知进入后台任务链路；关键任务持久化到 PostgreSQL，可通过租约、幂等键和有限重试在重启后恢复。
7. 各模式按自身最小执行间隔节流；告警生成标注证据图，通过 WebSocket 更新页面，并按规则投递企业微信。

## 技术栈

| 层级 | 技术 |
| --- | --- |
| 前端 | Vue 3、TypeScript、Vite |
| API | FastAPI、Pydantic、WebSocket |
| 数据库 | PostgreSQL 17、SQLAlchemy、Alembic |
| 队列 | Redis 8，异常时回退内存队列 |
| 视频 | FFmpeg、OpenCV |
| AI | Ultralytics YOLO、ByteTrack/轻量跟踪、OpenAI 兼容 VLM |
| 报表 | OpenPyXL、Excel/CSV 导出 |
| 监控 | Prometheus、Grafana（可选 profile） |
| 部署 | Docker Compose、CPU 基线镜像、NVIDIA 双 GPU 生产覆盖层 |

## 快速启动

### 1. 准备环境变量

```powershell
Copy-Item .env.example .env
```

至少修改：

```dotenv
POSTGRES_PASSWORD=请设置强密码
APP_ENCRYPTION_KEY=请设置稳定且唯一的加密密钥
ADMIN_USERNAME=admin
ADMIN_DISPLAY_NAME=系统管理员
ADMIN_PASSWORD=请设置至少8位密码
```

`APP_ENCRYPTION_KEY` 用于加密视频源凭据和模型 API Key。生产环境不要随意更换，否则已有密文将无法解密。

### 2. 开发环境

开发环境是基础 Compose 与开发覆盖层的叠加状态：

```powershell
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml up -d
```

- 前端开发服务器：<http://127.0.0.1:5174>
- 后端 API：<http://127.0.0.1:8100>
- FastAPI 文档：<http://127.0.0.1:8100/docs>

开发模式会挂载源码：后端使用 Uvicorn reload，前端使用 Vite HMR。修改业务源码通常无需重建镜像。

```powershell
# 查看状态
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml ps

# 查看日志
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml logs -f app web

# 重启前端开发容器
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml restart web

# 依赖或镜像配置变化后重建
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml up -d --build app web

# 停止服务
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml down
```

### 3. 生产环境

```powershell
docker compose -f compose.cpu.yml up -d --build
```

生产模式由后端在 8100 端口同时提供 API 和构建后的前端，访问 <http://127.0.0.1:8100>。

双 GPU 生产服务器使用基础 Compose 与 GPU 覆盖层：

```bash
docker compose -p yolo_vlm_monitor -f compose.cpu.yml -f compose.gpu.production.yml up -d --build
```

当前生产覆盖配置使用 `yolo26s.pt`，双 GPU 分担通用 YOLO，GPU 1 同时承担低频烟火检测；采集目标为 1 FPS，YOLO 批大小为 2，VLM 复核并发为 4。仅修改 Python 或部署配置时应使用 `bash scripts/update-gpu-production.sh`，前端也有变化时增加 `--frontend`。详细部署、回滚和容量边界见 [docs/GPU_PRODUCTION_229.md](docs/GPU_PRODUCTION_229.md)。

启用 Prometheus 与 Grafana：

```powershell
docker compose -f compose.cpu.yml --profile monitoring up -d
```

- Prometheus：<http://127.0.0.1:9090>
- Grafana：<http://127.0.0.1:3000>

完整启动、数据库迁移、旧版 Docker 兼容和故障排查见 [docs/STARTUP.md](docs/STARTUP.md)，抓帧容量验证见 [docs/SNAPSHOT_PREVIEW_OPS.md](docs/SNAPSHOT_PREVIEW_OPS.md)。

## 首次使用流程

1. 使用 `.env` 中的管理员账号登录。
2. 在“视频源”选择场景模板，填写视频地址和检测模式。
3. 连接成功后打开配置预览；岗位与入侵模式绘制 ROI，人流模式开启后自动工作。
4. 配置排班、节假日、阈值、冷却时间和抽帧周期。
5. 在“系统配置”检查本地模型，并配置和测试外部 VLM。
6. 在“企业微信机器人”添加群机器人并发送测试消息。
7. 先以少量摄像头影子运行，核对结果后再逐步扩容。

1 秒抽帧表示每秒调度一次单帧抓取与分析，不等同于保持连续的 1 FPS 解码流。该设置会明显增加 FFmpeg 连接、推理和数据库写入压力，建议仅用于少量重点摄像头并先压测。

## 主要 API

除 `/health`、登录接口和静态资源外，业务接口均需要会话认证。

| 模块 | 接口 |
| --- | --- |
| 登录与账号 | `/api/auth/captcha`、`/api/auth/login`、`/api/auth/me`、`/api/auth/logout`、`/api/auth/password`、`/api/users` |
| 摄像头 | `/api/cameras`、`/api/cameras/batch`、`/api/cameras/batch-move`、`/api/cameras/batch-off-duty-schedule`、`/api/cameras/batch-delete` |
| 摄像头目录 | `/api/camera-directories`、`/api/camera-directories/{id}` |
| 场景与能力 | `/api/scene-templates`、`/api/capabilities` |
| 摄像头配置 | `/api/cameras/{id}/modes`、`geometry`、`schedule`、`analyze` |
| 图像 | `/api/cameras/{id}/snapshot`、`preview/start`、`preview/heartbeat`、`preview/stop`、`preview` |
| 业务数据 | `/api/dashboard`、`/api/alerts`、`/api/analyses`、`/api/traffic`、`/api/traffic/summary`、`/api/traffic/monthly`、`/api/traffic/monthly/export` |
| 管理日志 | `/api/logs/audit`、`/api/logs/analyses`、`/api/logs/model-calls`、`/api/logs/{category}/{id}`、`/api/logs/{category}/export` |
| 系统设置 | `/api/settings/models`、`detectors`、`webhooks`、`retention`、`display` |
| 运行状态 | `/health`、`/api/runtime/workers`、`/metrics`、`/ws/events` |

完整请求结构和响应模型以 FastAPI 自动文档为准。

## 关键环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | Compose 自动配置 | PostgreSQL 连接地址 |
| `REDIS_URL` | `redis://redis:6379/0` | Redis 队列地址 |
| `APP_ENCRYPTION_KEY` | 开发占位值 | 密文加密密钥，生产必须修改 |
| `SESSION_IDLE_HOURS` | `8` | 会话闲置失效小时数 |
| `YOLO_MODEL_PATH` | `models/yolo26s.pt` | 通用 YOLO 权重 |
| `YOLO_DEVICE` | `cpu` | 通用模型设备 |
| `CAPTURE_FPS` | `1` | 常驻分析流每秒输出帧数 |
| `CAPTURE_MAX_HEIGHT` | `960` | 常驻分析流最大高度 |
| `CAPTURE_DECODE_DEVICES` | 空 | NVDEC 解码设备列表；生产为 `0,1` |
| `CAPTURE_GPU_STREAMS_PER_DEVICE` | `0` | 每张 GPU 分配的硬解视频流上限；生产为 `32` |
| `YOLO_IMGSZ` | `640` | 通用模型输入尺寸 |
| `YOLO_INFERENCE_PROCESSES` | `4` | 独立 YOLO 推理进程数；每个进程加载一份模型 |
| `YOLO_BATCH_SIZE` | `1` | 通用 YOLO 最大组批大小；生产为 `2` |
| `YOLO_BATCH_WAIT_MS` | `10` | 等待凑批的最大毫秒数 |
| `YOLO_THREADS_PER_PROCESS` | `5` | 每个 YOLO 推理进程使用的 CPU 线程数 |
| `YOLO_INTEROP_THREADS` | `1` | 每个进程的 PyTorch 算子间线程数 |
| `FIRE_SMOKE_MODEL` | `models/fire_smoke_yolov8.pt` | 烟火模型权重 |
| `ANALYSIS_WORKERS` | `10` | 抓图、入队和后处理的异步 worker 数量 |
| `FIRE_SMOKE_WORKERS` | `1` | 烟火 worker 数量 |
| `ANALYSIS_QUEUE_MAXSIZE` | `256` | 分析队列容量 |
| `ASYNC_CAPTURE_PERSISTENCE` | `false` | 异步保存快照和采集状态 |
| `ASYNC_POSTPROCESSING` | `false` | 启用持久化后台后处理链路，并自动启用异步采集持久化 |
| `BACKGROUND_QUEUE_CAPACITY` | `4096` | 后台关键任务队列容量 |
| `REVIEW_WORKERS` | `2` | VLM 复核并发数；当前生产覆盖为 `4` |
| `NOTIFICATION_WORKERS` | `4` | 外部通知 worker 数量 |
| `FRAME_CAPTURE_TIMEOUT_SECONDS` | `15` | 单帧抓取超时 |
| `MAX_LIVE_PREVIEWS` | `4` | 同时实时预览上限 |
| `LIVE_PREVIEW_FPS` | `2` | 实时预览输出帧率 |

完整配置来源和存储规则见 [docs/CONFIG_LOADING_AND_STORAGE.md](docs/CONFIG_LOADING_AND_STORAGE.md)。

## 数据持久化

Docker Compose 使用独立命名卷：

- `postgres_data`：账号、摄像头、配置、分析、告警和投递记录。
- `redis_data`：Redis AOF 数据。
- `evidence_data`：告警证据图片。
- `snapshot_data`：摄像头最近快照。
- `model_cache`：模型缓存。
- `grafana_data`：Grafana 数据。

通用和烟火 `.pt` 权重位于 `models/`，以只读方式挂载。`docker compose down` 不会删除命名卷；带 `-v` 的删除命令会清除卷，操作前必须备份。

## 项目目录

```text
backend/                 FastAPI、调度、队列、检测、告警和数据访问
backend/api/             登录、摄像头、监控、日志和设置接口
backend/detectors/       黑屏、通用 YOLO、烟火检测器
frontend/src/            Vue 管理界面
alembic/                 PostgreSQL 数据库迁移
deploy/                  Prometheus 与 Grafana 配置
docs/                    启动、运维、数据库和架构文档
models/                  本地模型权重
scripts/load_test.py     多摄像头负载测试
scripts/update-gpu-production.sh  GPU 生产构建、重启与健康检查
tests/                   后端测试
compose.cpu.yml          CPU 生产基线
compose.cpu.dev.yml      开发环境覆盖层
compose.gpu.production.yml  双 GPU 生产覆盖层
```

## 验证与上线建议

```powershell
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml ps
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml logs --tail=200 app web postgres redis
Invoke-RestMethod http://127.0.0.1:8100/health | ConvertTo-Json -Depth 8
```

正式告警前建议依次完成 10、32、64 路及最终规模的影子运行，持续观察抓帧成功率、队列深度、推理 P95、VLM 延迟、CPU/GPU/内存、告警准确率和不确定率。

生产 GPU 环境还应检查 `nvidia-smi`、`/health` 中每路 `decoder`/`frames`/`reconnects`、实际人员检测频率及后台任务积压。配置为 1 FPS 只代表采集目标，不代表每路所有模型都已稳定达到每秒一次；结论应以持续压测数据为准。

## 安全与使用限制

- 生产环境必须修改数据库密码、管理员密码和 `APP_ENCRYPTION_KEY`，并使用 HTTPS。
- 不要将真实 RTSP 密码、VLM API Key、企业微信 Webhook 或 `.env` 提交到版本库。
- 视频烟火预警是辅助检测手段，不能替代符合规范的消防报警设备。
- 烟火权重启动时进行 SHA256 校验，不匹配时拒绝加载，也不会自动联网下载。
- Ultralytics 开源版本涉及 AGPL-3.0；闭源商用前应完成许可证审查或购买适用许可。
- 人流与入侵依赖目标检测与跟踪，遮挡、镜面和摄像头抖动都会影响效果，必须用现场样本校准。
