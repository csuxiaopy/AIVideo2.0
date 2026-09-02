# AIVideo2.0 启动说明

本文分别说明本地开发环境和 CentOS 生产环境的首次启动、日常启动、验证和停止方式。

## 1. 启动前准备

### 1.1 必需模型

启动前确认项目中存在以下文件：

```text
models/yolo26s.pt
models/fire_smoke_yolov8.pt
```

- `yolo26s.pt` 是通用 COCO 检测模型。
- `fire_smoke_yolov8.pt` 是烟火专项权重，启动时会校验 SHA256。
- 两个权重均纳入项目 Git，通过 `git clone` 或 `git pull` 随代码到达开发机和服务器。
- 模型不会打入 Docker 镜像；Compose 将项目中的 `models/` 目录只读挂载到 app 容器。
- 模型不存在或校验失败时服务仍可启动，但 `/health` 会将对应检测器标记为非 `ready`，不能用于正式检测。

### 1.2 默认端口

| 服务 | 开发环境 | 生产环境 |
| --- | --- | --- |
| 前端 | `5173`（Vite） | `8100`（由后端提供已构建页面） |
| 后端 API | `8100` | `8100` |
| PostgreSQL | 容器网络 `postgres:5432` | 容器网络 `postgres:5432` |
| Redis | 容器网络 `redis:6379` | 容器网络 `redis:6379` |

## 2. 本地开发环境（全 Docker）

本地开发不需要在宿主机安装 Python、Node.js、pnpm、FFmpeg、PostgreSQL 或 Redis。Docker Compose 一次启动以下服务：

| 服务 | 容器职责 | 宿主机访问 |
| --- | --- | --- |
| `postgres` | PostgreSQL 17 开发数据库 | 不暴露端口，仅容器网络访问 |
| `redis` | 任务队列和缓存 | 不暴露端口，仅容器网络访问 |
| `app` | FastAPI、YOLO、FFmpeg，后端源码热重载 | <http://127.0.0.1:8100> |
| `web` | Vite + Vue，前端源码热更新 | <http://127.0.0.1:5173> |

### 2.1 软件要求

- Docker Desktop（Windows）
- Docker Compose v2

```powershell
Set-Location 'D:\program\AIvideo\AIVideo2.0'
docker version
docker compose version
```

### 2.2 创建开发配置

```powershell
Set-Location 'D:\program\AIvideo\AIVideo2.0'
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

编辑 `.env`，至少确认以下配置：

```dotenv
POSTGRES_PASSWORD=monitor_pass
APP_ENCRYPTION_KEY=development-only-change-me

YOLO_MODEL_PATH=models/yolo26s.pt
YOLO_DEVICE=cpu
YOLO_IMGSZ=640
YOLO_CONFIDENCE=0.35
YOLO_IOU=0.5
YOLO_INFERENCE_TIMEOUT_SECONDS=30

ANALYSIS_WORKERS=2
FIRE_SMOKE_WORKERS=1
ANALYSIS_QUEUE_MAXSIZE=256
FRAME_CAPTURE_TIMEOUT_SECONDS=15
```

开发环境中的 `DATABASE_URL` 和 `REDIS_URL` 由 `compose.cpu.yml` 自动注入，分别指向容器网络内的 `postgres` 和 `redis`，无需改成本机地址。

### 2.3 检查模型

```powershell
Test-Path .\models\yolo26s.pt
Test-Path .\models\fire_smoke_yolov8.pt
Get-ChildItem .\models\*.pt
(Get-FileHash .\models\yolo26s.pt -Algorithm SHA256).Hash
(Get-FileHash .\models\fire_smoke_yolov8.pt -Algorithm SHA256).Hash
```

两个 `Test-Path` 均应返回 `True`。预期 SHA256：

```text
yolo26s.pt
646f8bc3fe0a656803d95c294f7852321748cb29d13466a1af8862e2db384a1b

fire_smoke_yolov8.pt
ac0a10257b2bc1f20c9d957f8adeeb61dd6140322fc19d0b4a116cb491776d16
```

权重随 Git 获取，但不会进入 Docker 镜像；`models/` 会只读挂载到 app 容器的 `/app/models`。

### 2.4 首次构建并启动全部服务

```powershell
Set-Location 'D:\program\AIvideo\AIVideo2.0'
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml config
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml build app
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml up -d
```

首次启动会构建前后端生产基础镜像，并创建 PostgreSQL、Redis、告警证据、快照和前端依赖数据卷。`web` 容器会自行执行 `pnpm install --frozen-lockfile` 后启动 Vite。

### 2.5 查看启动状态

```powershell
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml ps
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml logs --tail=200 app
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml logs --tail=100 web
```

预期 `postgres`、`redis`、`app`、`web` 都是 `Up`，app 最终显示 `healthy`。

浏览器访问：

- 开发页面：<http://127.0.0.1:5173>
- 后端页面：<http://127.0.0.1:8100>
- 健康检查：<http://127.0.0.1:8100/health>
- API 文档：<http://127.0.0.1:8100/docs>
- Prometheus 指标：<http://127.0.0.1:8100/metrics>

Vite 在容器网络中把 `/api`、`/health`、`/evidence` 和 `/ws` 代理到 `app:8100`。

### 2.6 热更新范围

- 修改 `backend/`、`alembic/` 或 `run.py`：app 容器中的 Uvicorn 自动重载。
- 修改 `frontend/src/`：web 容器中的 Vite 自动热更新页面。
- 修改 `requirements*.txt`、`Dockerfile.cpu`：需要重新构建 app。
- 修改 `frontend/package.json` 或 `pnpm-lock.yaml`：建议重建 web 的依赖卷或重新创建 web 容器。
- 修改 `.env` 或 Compose 文件：执行一次 `up -d` 重新创建受影响容器。

### 2.7 日常启动和验证

```powershell
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml up -d
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml ps
Invoke-RestMethod http://127.0.0.1:8100/health | ConvertTo-Json -Depth 8
```

健康响应中重点检查：

```text
detectors.general.status = ready
detectors.general.model = models/yolo26s.pt
detectors.general.device = cpu
detectors.general.imgsz = 640
detectors.general.iou = 0.5
```

### 2.8 查看实时日志

```powershell
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml logs -f --tail=200 app web
```

### 2.9 停止开发环境

停止并移除本项目容器和网络，但保留数据库及证据数据卷：

```powershell
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml down
```

不要使用 `down -v`，否则会删除 PostgreSQL、Redis、告警证据、快照和前端依赖数据卷。

## 3. 生产环境（CentOS 7 + Docker Compose）

生产基线使用 `compose.cpu.yml`，运行 app、PostgreSQL 和 Redis。模型目录通过只读卷挂载，不会被打进镜像。

### 3.1 软件和目录要求

- Docker Engine
- Docker Compose v2（必须支持 `docker compose`；不支持仅替换为旧版 `docker-compose`）
- 项目目录建议为 `/data/yolo_vlm_monitor`
- 确保 8100 端口未被其他服务占用

```bash
cd /data/yolo_vlm_monitor
docker version
docker compose version
```

### 3.2 创建生产配置

```bash
cd /data/yolo_vlm_monitor
umask 077
cp -n .env.example .env
vi .env
docker compose -f compose.cpu.yml config >/dev/null
```

至少配置以下内容：

```dotenv
POSTGRES_PASSWORD=替换为数据库强密码
APP_ENCRYPTION_KEY=替换为至少32位的随机密钥

YOLO_MODEL_PATH=models/yolo26s.pt
YOLO_DEVICE=cpu
YOLO_IMGSZ=640
YOLO_CONFIDENCE=0.35
YOLO_IOU=0.5
YOLO_INFERENCE_TIMEOUT_SECONDS=30

ANALYSIS_WORKERS=2
FIRE_SMOKE_WORKERS=1
ANALYSIS_QUEUE_MAXSIZE=256
FRAME_CAPTURE_TIMEOUT_SECONDS=15
```

注意：

- Compose 会覆盖 `.env` 中的 `DATABASE_URL` 和 `REDIS_URL`，app 使用容器内 PostgreSQL、Redis。
- `APP_ENCRYPTION_KEY` 用于加密 RTSP 密码和外部模型 API Key。投入使用后不能随意更换。
- 首次上线先使用企业微信机器人的连接测试和告警中心手动发送验证，再勾选需要自动发送的告警级别。
- `POSTGRES_PASSWORD` 只在首次创建数据库卷时用于初始化。已有数据库不能只改 `.env` 密码。

### 3.3 检查 Git 中的模型

模型随项目 Git 仓库到达服务器，不需要另外下载或复制。更新代码后先确认 Git 已完整取得两个权重：

```bash
cd /data/yolo_vlm_monitor
test -s models/yolo26s.pt
test -s models/fire_smoke_yolov8.pt
ls -lh models/yolo26s.pt models/fire_smoke_yolov8.pt
sha256sum models/yolo26s.pt
sha256sum models/fire_smoke_yolov8.pt
```

预期 SHA256：

```text
646f8bc3fe0a656803d95c294f7852321748cb29d13466a1af8862e2db384a1b  models/yolo26s.pt
ac0a10257b2bc1f20c9d957f8adeeb61dd6140322fc19d0b4a116cb491776d16  models/fire_smoke_yolov8.pt
```

任一文件缺失或哈希不一致时停止部署，不要启动或重启生产 app。

### 3.4 首次构建和启动

```bash
cd /data/yolo_vlm_monitor
docker compose -f compose.cpu.yml config >/dev/null
docker compose -f compose.cpu.yml build app
docker compose -f compose.cpu.yml up -d
```

不要使用 `down -v`，它会删除本项目的 PostgreSQL、Redis、告警证据和快照数据卷。

### 3.5 启动验证

```bash
docker compose -f compose.cpu.yml ps
docker compose -f compose.cpu.yml logs --tail=200 app
curl -fsS http://127.0.0.1:8100/health
curl -fsS http://127.0.0.1:8100/metrics >/dev/null
```

验收要求：

- `postgres`、`redis` 和 `app` 均为 `Up`，app 最终为 `healthy`。
- `/health` 返回 HTTP 200。
- `detectors.general.status` 为 `ready`，模型为 `models/yolo26s.pt`，设备为 `cpu`。
- `detectors.fire_smoke.status` 必须为 `ready`，并显示正确的烟火权重 SHA256。
- 日志中没有持续出现模型加载、数据库连接或 FFmpeg 错误。

浏览器访问 `http://服务器IP:8100`。生产环境应只在隔离内网开放，或置于带认证和 TLS 的受控反向代理后面。

### 3.6 日常启动、停止和重启

启动全部服务：

```bash
cd /data/yolo_vlm_monitor
docker compose -f compose.cpu.yml up -d
```

只重启应用，不重启数据库和 Redis：

```bash
docker compose -f compose.cpu.yml restart app
```

停止服务但保留容器和数据：

```bash
docker compose -f compose.cpu.yml stop
```

重新启动已停止的服务：

```bash
docker compose -f compose.cpu.yml start
```

持续查看 app 日志：

```bash
docker compose -f compose.cpu.yml logs -f --tail=200 app
```

### 3.7 代码更新后的发布

```bash
cd /data/yolo_vlm_monitor
docker compose -f compose.cpu.yml config >/dev/null
docker compose -f compose.cpu.yml build app
docker compose -f compose.cpu.yml up -d
docker compose -f compose.cpu.yml ps
docker compose -f compose.cpu.yml logs --tail=200 app
curl -fsS http://127.0.0.1:8100/health
```

只有后端代码临时联调时，可以叠加开发覆盖层：

```bash
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml up -d
```

该模式会挂载本地后端源码并开启 Uvicorn 热重载，同时启动 Vite 服务 `5173`。正式生产发布不要叠加 `compose.cpu.dev.yml`。

### 3.8 可选监控服务

```bash
docker compose -f compose.cpu.yml --profile monitoring up -d
```

- Prometheus：`http://服务器IP:9090`
- Grafana：`http://服务器IP:3000`

## 4. 常见启动问题

### 模型显示 degraded

```bash
docker compose -f compose.cpu.yml exec -T app ls -lh /app/models
docker compose -f compose.cpu.yml logs --tail=200 app
```

确认宿主机文件名、`.env` 中的 `YOLO_MODEL_PATH` 和容器内路径一致。

### app 不断重启

```bash
docker compose -f compose.cpu.yml ps
docker compose -f compose.cpu.yml logs --tail=300 app
docker compose -f compose.cpu.yml logs --tail=100 postgres
docker compose -f compose.cpu.yml logs --tail=100 redis
```

优先检查 PostgreSQL 密码、数据库卷是否已用旧密码初始化、模型加载和旧 Docker seccomp 兼容问题。

### 摄像头一直离线

从服务器宿主机确认 RTSP 地址和端口可达，并注意密码中的 `@` 应编码为 `%40`。系统周期任务使用短生命周期 FFmpeg 抓取单帧；单路超时不会阻塞其他摄像头。

更多 CentOS 7、旧 Docker、备份及故障处理细节见 [CPU_DOCKER_OPS_GUIDE.md](CPU_DOCKER_OPS_GUIDE.md)。
