# 监衡：YOLO + 视觉大模型视频监控平台

监衡是一套面向营业厅、工位、出入口和库房的视频智能分析平台。系统通过 FFmpeg 周期抽帧，结合本地 YOLO、像素统计、目标跟踪和外部视觉大模型，实现黑屏、离岗、在岗、人流、玩手机、吸烟、烟火和区域入侵检测，并提供告警留证、企业微信推送、账号权限和运行状态监控。

项目以 96 路 RTSP 接入为设计目标。实际可承载路数取决于视频编码、抽帧周期、CPU/GPU、模型大小和外部 VLM 延迟，正式上线前必须使用现场码流压测。

## 核心能力

- 支持 RTSP、RTSPS、RTMP、HTTP、HTTPS 和 `file://` 视频源。
- 每个摄像头可独立组合八类检测模式。
- 内置员工工位、客户区/入口、库房/安全区场景模板。
- 支持每 1、5、10、20、30、60、120 秒抓取一帧。
- 可在实时预览上绘制岗位 ROI、人流统计线和入侵禁区。
- 烟火任务使用独立队列和 worker，避免被普通任务阻塞。
- 保存带标注的告警证据，支持筛选、清理和手动补发。
- 支持多个企业微信机器人、分级推送和失败重试。
- 提供人流趋势、当前人数、摄像头排名和实时事件更新。
- 提供管理员/普通用户权限、健康检查和 Prometheus 指标。

## 功能模块

### 1. 账号与权限

系统不开放注册。首次启动且数据库中没有账号时，根据以下环境变量创建首个管理员：

- `ADMIN_USERNAME`：管理员登录名。
- `ADMIN_DISPLAY_NAME`：管理员显示名称。
- `ADMIN_PASSWORD`：初始密码，要求 8–128 位。

管理员可以创建普通用户、修改显示名称、启停账号、重置密码和删除账号。普通用户可以访问监控总览、人流报表和告警中心，但不能管理摄像头、模型、Webhook、保留策略或账号。

会话连续闲置 8 小时后默认失效。修改或重置密码、停用或删除账号时，该账号已有会话会失效。HTTPS 部署应设置 `SECURE_COOKIES=true`，并使用 `ALLOWED_ORIGINS` 限制 WebSocket 来源。

### 2. 监控总览

总览页集中展示摄像头总数与在线状态、今日告警、烟火紧急告警、最近快照、抽帧周期、启用模式、最近告警和 worker 状态。当前在店人数可在系统配置中隐藏。

监控矩阵默认显示后台周期快照，不会为所有摄像头自动开启连续视频流，从而减少连接与解码开销。

### 3. 视频源管理

管理员可以单个或批量添加视频源，并配置：

- 业务 ID、显示名称、视频地址和启用状态。
- 场景类型与检测模式组合。
- 1、5、10、20、30、60、120 秒抽帧周期。
- 岗位区域、跨线统计线和入侵禁区。
- 周排班、多个班次、时区和节假日。
- 离岗时长、班次宽限、告警冷却及检测阈值。

编辑时已保存的视频地址不会回显；留空表示保持原地址，填写新地址才会替换。API 和日志只返回脱敏地址，凭据使用 `APP_ENCRYPTION_KEY` 加密存储。RTSP 密码中的 `@` 应编码为 `%40`。

实时预览采用按需租约：仅打开预览时建立连续取流。预览数量、帧率和超时分别由 `MAX_LIVE_PREVIEWS`、`LIVE_PREVIEW_FPS`、`LIVE_PREVIEW_TIMEOUT_SECONDS` 控制。

### 4. 场景模板与空间配置

| 场景模板 | 默认用途 | 默认模式 | 必需图形 |
| --- | --- | --- | --- |
| 员工工位 | 行为规范和离岗管理 | 离岗、玩手机、黑屏 | 岗位 ROI |
| 客户区/入口 | 营业厅客流统计 | 人流、黑屏 | 人流统计线 |
| 库房/安全区 | 全天安全检测 | 烟火、区域入侵、黑屏 | 入侵禁区 |

岗位 ROI 和禁区至少需要 3 个归一化坐标点，人流线必须包含 2 个点。烟火、区域入侵和黑屏属于全天安全模式，不受普通排班限制。

### 5. 智能检测

| 模式 | 实现与判定 | 结果 |
| --- | --- | --- |
| 黑屏 | OpenCV 灰度统计；默认 `mean≤18`、`std≤12`、近黑比例 `≥0.92`，连续 3 帧异常后确认 | `high` 告警 |
| 离岗 | 排班内检测人员框与岗位 ROI 是否相交；持续无人达到 `off_duty_seconds` 并考虑班次宽限 | `normal` 告警 |
| 在岗记录 | 人员框与岗位 ROI 有任意交叠即判定在岗 | 仅记录 |
| 人流 | YOLO 人员检测和独立跟踪，按脚点跨线方向累计进入/离开 | 仅统计 |
| 玩手机 | 外部 VLM 分析当前帧，要求明确操作或注视手机 | `normal` 告警 |
| 吸烟 | 外部 VLM 分析持烟、吸食动作或可关联烟雾 | `normal` 告警 |
| 烟火 | 独立 YOLO；火焰连续 2 帧命中，烟雾最近 5 帧至少 3 帧命中 | `critical` 告警 |
| 区域入侵 | 人员跟踪脚点进入禁区即触发，按跟踪 ID 去重 | `high` 告警 |

玩手机和吸烟共用一次联合 VLM 请求：经济模型先筛查；只要任一行为不是 `none`，便使用同一帧和全部启用行为交给增强模型复核。两项同时确认时分别生成告警，但共用原始证据帧。

通用 YOLO 使用 COCO 预训练模型识别人。安装完整跟踪依赖时，每个摄像头使用独立 ByteTrack；依赖不可用时回退到轻量质心跟踪器，并在运行状态中标记降级。

### 6. 告警中心

告警中心支持按检测模式和严重级别组合筛选，可查看摄像头、触发时间、置信度、判定原因和证据图片。用户可以多选历史告警发送到指定机器人；管理员还可按保留天数清理告警记录及证据。

普通模式使用摄像头级 `alert_cooldown_seconds` 限频，默认 300 秒；烟火和区域入侵使用各自的专用冷却规则。

### 7. 人流报表

人流模块基于跟踪 ID 跨线事件，展示今日总人流、进入与离开数量、当前在店人数、当日趋势、当前人数排名、今日人流排名以及逐摄像头统计。

系统配置可以隐藏人流报表菜单或总览中的当前人数。隐藏只影响界面展示，不会停止后台统计或删除历史数据。

### 8. 企业微信机器人

管理员可维护多个企业微信群机器人，每个目标可独立启停，并选择自动接收的 `normal`、`high`、`critical` 级别。

投递时先发送 Markdown 摘要，再发送 Base64 + MD5 格式的证据图片。图片超过 2 MB 时在内存中压缩，不改写原证据文件。HTTP 错误或企业微信返回非零 `errcode` 时指数退避，最多尝试 5 次；两条消息都成功才计为投递成功。

### 9. 系统配置

- 界面展示：控制人流报表和当前人数是否显示。
- 数据保留：设置告警保留天数和自动清理开关。
- 外部视觉模型：配置 OpenAI 兼容 Base URL、API Key、经济模型和增强模型，并测试连接。
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
                                             ├── 通用 YOLO + 跟踪
                                             └── 外部 VLM
                                                      │
                                                      ▼
                         PostgreSQL ← 分析记录 / 告警 / 配置
                                                      │
                             证据图片 + WebSocket + 企业微信
```

1. 调度器按每路摄像头的 `frame_interval_seconds` 触发任务，同周期摄像头错峰执行。
2. 任务进入 Redis 优先级队列；Redis 不可用时降级为进程内队列。
3. 烟火使用独立队列和 worker，其余任务按安全性和业务类型确定优先级。
4. FFmpeg 以短生命周期进程抓取单张 JPEG，抓取完成后退出。
5. 各模式按自身最小执行间隔节流，分析结果写入 PostgreSQL。
6. 告警生成标注证据图，通过 WebSocket 更新页面，并按规则投递企业微信。

## 技术栈

| 层级 | 技术 |
| --- | --- |
| 前端 | Vue 3、TypeScript、Vite |
| API | FastAPI、Pydantic、WebSocket |
| 数据库 | PostgreSQL 17、SQLAlchemy、Alembic |
| 队列 | Redis 8，异常时回退内存队列 |
| 视频 | FFmpeg、OpenCV |
| AI | Ultralytics YOLO、ByteTrack/轻量跟踪、OpenAI 兼容 VLM |
| 监控 | Prometheus、Grafana（可选 profile） |
| 部署 | Docker Compose、CPU 基线镜像 |

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
3. 连接成功后打开配置预览，绘制岗位 ROI、人流线或入侵禁区。
4. 配置排班、节假日、阈值、冷却时间和抽帧周期。
5. 在“系统配置”检查本地模型，并配置和测试外部 VLM。
6. 在“企业微信机器人”添加群机器人并发送测试消息。
7. 先以少量摄像头影子运行，核对结果后再逐步扩容。

1 秒抽帧表示每秒调度一次单帧抓取与分析，不等同于保持连续的 1 FPS 解码流。该设置会明显增加 FFmpeg 连接、推理和数据库写入压力，建议仅用于少量重点摄像头并先压测。

## 主要 API

除 `/health`、登录接口和静态资源外，业务接口均需要会话认证。

| 模块 | 接口 |
| --- | --- |
| 登录与账号 | `/api/auth/login`、`/api/auth/me`、`/api/auth/logout`、`/api/auth/password`、`/api/users` |
| 摄像头 | `/api/cameras`、`/api/cameras/batch`、`/api/scene-templates`、`/api/capabilities` |
| 摄像头配置 | `/api/cameras/{id}/modes`、`geometry`、`schedule`、`analyze` |
| 图像 | `/api/cameras/{id}/snapshot`、`preview/start`、`preview/heartbeat`、`preview/stop`、`preview` |
| 业务数据 | `/api/dashboard`、`/api/alerts`、`/api/analyses`、`/api/traffic`、`/api/traffic/summary` |
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
| `YOLO_IMGSZ` | `640` | 通用模型输入尺寸 |
| `FIRE_SMOKE_MODEL` | `models/fire_smoke_yolov8.pt` | 烟火模型权重 |
| `ANALYSIS_WORKERS` | `2` | 普通分析 worker 数量 |
| `FIRE_SMOKE_WORKERS` | `1` | 烟火 worker 数量 |
| `ANALYSIS_QUEUE_MAXSIZE` | `256` | 分析队列容量 |
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
backend/api/             登录、摄像头、监控和设置接口
backend/detectors/       黑屏、通用 YOLO、烟火检测器
frontend/src/            Vue 管理界面
alembic/                 PostgreSQL 数据库迁移
deploy/                  Prometheus 与 Grafana 配置
docs/                    启动、运维、数据库和架构文档
models/                  本地模型权重
scripts/load_test.py     多摄像头负载测试
tests/                   后端测试
compose.cpu.yml          CPU 生产基线
compose.cpu.dev.yml      开发环境覆盖层
```

## 验证与上线建议

```powershell
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml ps
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml logs --tail=200 app web postgres redis
Invoke-RestMethod http://127.0.0.1:8100/health | ConvertTo-Json -Depth 8
```

正式告警前建议依次完成 10、32、64 路及最终规模的影子运行，持续观察抓帧成功率、队列深度、推理 P95、VLM 延迟、CPU/GPU/内存、告警准确率和不确定率。

## 安全与使用限制

- 生产环境必须修改数据库密码、管理员密码和 `APP_ENCRYPTION_KEY`，并使用 HTTPS。
- 不要将真实 RTSP 密码、VLM API Key、企业微信 Webhook 或 `.env` 提交到版本库。
- 视频烟火预警是辅助检测手段，不能替代符合规范的消防报警设备。
- 烟火权重启动时进行 SHA256 校验，不匹配时拒绝加载，也不会自动联网下载。
- Ultralytics 开源版本涉及 AGPL-3.0；闭源商用前应完成许可证审查或购买适用许可。
- 人流与入侵依赖目标跟踪，遮挡、镜面、摄像头抖动和跨线位置都会影响效果，必须用现场样本校准。
