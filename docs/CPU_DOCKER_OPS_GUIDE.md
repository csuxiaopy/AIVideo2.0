# YOLO VLM Monitor CPU 版运维教程

本文适用于当前服务器环境：

- 服务器：`172.16.166.229`
- 系统：CentOS 7
- Docker：19.03.13（旧环境，见 §8 seccomp 兼容说明）
- 命令形式：`docker compose`（v2）；当前 Compose 文件不承诺兼容旧版 `docker-compose`（v1）
- 项目目录：`/data/yolo_vlm_monitor`
- 部署文件：`compose.cpu.yml`
- Web/API 端口：`8100`
- 无 GPU，使用 CPU-only PyTorch

## 1. 安全红线

服务器上还有其他老业务，日常操作必须限制在本项目内。

禁止执行：

```bash
docker system prune
docker container prune
docker image prune
docker volume prune
docker network prune
rm -rf /data/docker_path/docker_data
docker compose -f compose.cpu.yml down -v
```

不要升级或重启 Docker daemon，除非已经评估对老业务的影响并获得明确确认。不要删除名称不以 `yolo_vlm_monitor` 开头的容器、镜像、网络或 volume。

## 2. 项目组成

默认执行 `docker compose -f compose.cpu.yml up -d` 会运行：

| 服务 | Compose 服务名 | 用途 | 宿主机端口 |
| --- | --- | --- | --- |
| app | `app` | 前端、API、YOLO 分析 | `8100` |
| postgres | `postgres` | 数据库（PostgreSQL 17） | 不对外暴露 |
| redis | `redis` | 队列和缓存 | 不对外暴露 |

前端已编译到 app 镜像的 `/app/frontend/dist`，不需要单独启动 Node.js 容器。访问 `/` 是前端页面，访问 `/health` 是健康接口。

Prometheus 和 Grafana 使用 `monitoring` profile，默认不会启动。

数据持久化全部落在 Docker volume（重建容器不丢）：

```text
postgres_data    PostgreSQL 数据
redis_data       Redis 持久化
evidence_data    app 容器 /app/data/evidence 火烟检测证据
snapshot_data    app 容器 /app/data/snapshots 场景快照
```

> 注：开发和生产数据库统一使用 PostgreSQL。生产环境由 `compose.cpu.yml` 注入 `DATABASE_URL=postgresql+psycopg://monitor:***@postgres:5432/monitor`。

## 3. 登录服务器

在 Windows PowerShell 中执行：

```powershell
ssh root@172.16.166.229
```

登录后进入项目：

```bash
cd /data/yolo_vlm_monitor
pwd
```

## 4. 每次操作前的只读检查

```bash
cd /data/yolo_vlm_monitor
docker --version
docker compose version
docker compose -f compose.cpu.yml ps
docker ps
ss -lntp | grep ':8100 ' || true
df -h /data /data/docker_path/docker_data
free -h
```

确认以下事项后再继续：

1. Docker 和 Compose 版本正常。
2. 8100 没有被其他业务占用，或占用者就是本项目 app。
3. 老业务容器仍在运行。
4. 磁盘和内存充足。

## 5. 配置生产环境变量

Compose 支持从项目目录的 `.env` 读取变量。首次部署前必须创建 `.env`，至少修改数据库密码和加密密钥：

```bash
cd /data/yolo_vlm_monitor
umask 077
cp -n .env.example .env
vi .env
docker compose -f compose.cpu.yml config >/dev/null
```

必须修改的两项（默认值不能用于生产）：

```dotenv
POSTGRES_PASSWORD=请替换为强密码          # PostgreSQL 密码，compose 用它初始化数据库并注入 app
APP_ENCRYPTION_KEY=请替换为足够长的随机密钥  # 加密 API Key 与 RTSP 密码
```

其他常用配置：

```dotenv
YOLO_MODEL_PATH=models/yolo26s.pt
YOLO_DEVICE=cpu
YOLO_IMGSZ=640
YOLO_CONFIDENCE=0.35
YOLO_IOU=0.5
FIRE_SMOKE_MODEL=models/fire_smoke_yolov8.pt
FIRE_SMOKE_DEVICE=cpu
ANALYSIS_WORKERS=2
FIRE_SMOKE_WORKERS=1
SHADOW_MODE=true
```

镜像源变量（均有 daocloud 国内默认值，一般无需设置，仅在特殊网络环境覆盖）：

```dotenv
POSTGRES_IMAGE=      # postgres 镜像
REDIS_IMAGE=         # redis 镜像
NODE_IMAGE=          # 开发模式 web 服务镜像
PROMETHEUS_IMAGE=    # monitoring 镜像
GRAFANA_IMAGE=       # monitoring 镜像
GRAFANA_PASSWORD=    # Grafana 登录密码
```

注意：

- `POSTGRES_PASSWORD` 只在**首次创建 postgres_data 卷**时生效。如果数据库已经用旧密码初始化，不能只修改 `.env`；还需在数据库中同步修改用户密码，否则 app 将无法连接数据库。
- 修改 `APP_ENCRYPTION_KEY` 后，既有 API Key 和 RTSP 密码将无法解密，需重新配置。

## 6. 检查模型文件

两个模型权重均提交在项目 Git 仓库中，服务器通过 `git clone` 或 `git pull` 与代码一起取得，不需要单独下载。拉取代码后执行：

```bash
cd /data/yolo_vlm_monitor
ls -lh models
sha256sum models/yolo26s.pt
sha256sum models/fire_smoke_yolov8.pt
```

当前预期 SHA256：

```text
646f8bc3fe0a656803d95c294f7852321748cb29d13466a1af8862e2db384a1b  models/yolo26s.pt
ac0a10257b2bc1f20c9d957f8adeeb61dd6140322fc19d0b4a116cb491776d16  models/fire_smoke_yolov8.pt
```

任一文件缺失或哈希不一致时停止部署。权重虽然纳入 Git，但仍被 `.dockerignore` 排除在镜像构建上下文之外；Compose 将项目 `models` 目录只读挂载到 app，不要在容器内修改该目录。

## 7. 构建 app 镜像

先备份 Dockerfile：

```bash
cd /data/yolo_vlm_monitor
cp -a Dockerfile.cpu "Dockerfile.cpu.bak.$(date +%Y%m%d_%H%M%S)"
```

构建：

```bash
docker compose -f compose.cpu.yml build app
```

成功标志：

```text
Image <项目名>-app Built
```

当前 Dockerfile 两阶段构建要点：

- **web 阶段**：`node:22-bookworm-slim`（非 alpine，规避 musl/native 依赖），`corepack enable` + `pnpm --frozen-lockfile` 安装前端依赖，`npm_config_node_linker=hoisted` 平铺安装（规避 overlayfs 上 pnpm symlink 的 EPERM），`pnpm run build` 产出 `frontend/dist`。
- **app 阶段**：`python:3.12-slim`，apt 走阿里云镜像；PyTorch 从官方 CPU wheel 索引安装（不拉 CUDA/CUDNN）；pip 关闭 Rich 进度条（`--progress-bar off`），避免旧 seccomp 下创建刷新线程失败；依赖独立成层，利用构建缓存。
- 镜像源：daocloud（基础镜像）/ npmmirror（corepack/pnpm）/ aliyun（apt、pip）/ pytorch CPU 官方（torch）。

如果服务器镜像已存在且依赖无变化，可以跳过构建直接 `up -d`（见 §9）。

## 8. 旧 Docker 的 seccomp 兼容说明

当前 `python:3.12-slim` 是 Debian 13。Docker 19 的默认 seccomp 与其线程创建存在兼容问题，直接执行 apt/dpkg 或启动 Python 多线程服务可能出现：

```text
lzma error: Cannot allocate memory
RuntimeError: can't start new thread
```

这不代表宿主机真的缺少内存。本项目已做两层处理：

1. **运行时**：`compose.cpu.yml` 的 app 服务带 `security_opt: seccomp=unconfined`，兼容旧 Docker；新环境（Docker 20.10+ / containerd）可删除该段。
2. **构建时**：若在 Docker 19 上 `docker compose build app` 于 apt/pip 阶段报上述错误，用下面的**本地基础镜像兜底方案**（构建一次，之后 Dockerfile 无需改动）：

```bash
docker image inspect yolo_vlm_monitor_python_ffmpeg:3.12-slim >/dev/null 2>&1
echo $?   # 返回 0 表示已有基础镜像，跳过构建
```

不存在时先确认临时容器名未被占用：

```bash
docker ps -a --filter name='^/yolo_vlm_monitor_cpu_base_builder$'
```

没有输出时执行：

```bash
docker run --name yolo_vlm_monitor_cpu_base_builder \
  --security-opt seccomp=unconfined \
  python:3.12-slim \
  sh -lc 'export DEBIAN_FRONTEND=noninteractive; \
    sed -i "s|http://deb.debian.org|http://mirrors.aliyun.com|g" /etc/apt/sources.list.d/debian.sources; \
    rm -f /etc/apt/apt.conf.d/docker-clean; \
    apt-get update; \
    apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*'
```

确认退出码为 `0`，提交项目专用镜像并清理临时容器：

```bash
docker inspect yolo_vlm_monitor_cpu_base_builder \
  --format 'ExitCode={{.State.ExitCode}} Status={{.State.Status}}'
docker commit --change 'CMD ["python3"]' \
  yolo_vlm_monitor_cpu_base_builder \
  yolo_vlm_monitor_python_ffmpeg:3.12-slim
docker rm yolo_vlm_monitor_cpu_base_builder
docker run --rm yolo_vlm_monitor_python_ffmpeg:3.12-slim ffmpeg -version
```

不要给原始 `python:3.12-slim` 重新打覆盖性 tag，以免影响其他项目。

## 9. 启动项目

```bash
cd /data/yolo_vlm_monitor
docker compose -f compose.cpu.yml up -d
```

这条命令会创建缺失容器或更新发生变化的项目容器，不会删除其他业务容器。`compose.cpu.yml` 已用 `POSTGRES_PASSWORD:?` 强制要求 `.env` 中存在该变量，未配置时命令会直接报错提示。

检查状态：

```bash
docker compose -f compose.cpu.yml ps
```

预期 app、postgres、redis 三个服务均为 `Up`，并最终显示 `healthy`。

## 10. 访问前端和 API

服务器内部验证：

```bash
curl -I http://127.0.0.1:8100/
curl -sS http://127.0.0.1:8100/health
```

Windows 与服务器网络互通且防火墙允许 8100 时，浏览器访问：

```text
http://172.16.166.229:8100/
```

注意：Windows 浏览器中的 `127.0.0.1` 指 Windows 本机，不是远程 Linux 服务器。

如果不希望开放服务器端口，可在 Windows PowerShell 建立 SSH 隧道：

```powershell
ssh -N -L 8100:127.0.0.1:8100 root@172.16.166.229
```

保持该窗口运行，然后在 Windows 浏览器访问：

```text
http://127.0.0.1:8100/
```

健康接口正常时会返回 HTTP 200，并包含：

```json
{
  "status": "ok"
}
```

## 11. 日常启停和重启

查看状态：

```bash
docker compose -f compose.cpu.yml ps
```

启动全部默认服务：

```bash
docker compose -f compose.cpu.yml up -d
```

只重启 app：

```bash
docker compose -f compose.cpu.yml restart app
```

停止本项目容器但保留数据：

```bash
docker compose -f compose.cpu.yml stop
```

重新启动已停止的项目容器：

```bash
docker compose -f compose.cpu.yml start
```

不要使用 `down -v`，它会删除项目数据 volume。

## 12. 查看日志

app 最近 100 行：

```bash
docker compose -f compose.cpu.yml logs --tail=100 app
```

持续跟踪 app：

```bash
docker compose -f compose.cpu.yml logs -f --tail=100 app
```

查看所有项目服务：

```bash
docker compose -f compose.cpu.yml logs --tail=100
```

退出实时日志按 `Ctrl+C`，不会停止容器。

## 13. 更新代码后的标准发布流程

```bash
cd /data/yolo_vlm_monitor
docker compose -f compose.cpu.yml config >/dev/null
docker compose -f compose.cpu.yml build app
docker compose -f compose.cpu.yml up -d
docker compose -f compose.cpu.yml ps
docker compose -f compose.cpu.yml logs --tail=100 app
curl -sS --max-time 10 http://127.0.0.1:8100/health
```

如果只修改了 `compose.cpu.yml`，也应先运行 `config` 检查 YAML 和变量解析。

如果构建失败，不要立即清缓存或 prune。先保留完整错误信息：

```bash
docker compose -f compose.cpu.yml build app 2>&1 | tee build-app.log
```

`build-app.log` 位于项目目录，便于定位最后一个失败步骤。

## 14. 数据备份

### 14.1 PostgreSQL 逻辑备份

```bash
cd /data/yolo_vlm_monitor
mkdir -p backups
docker compose -f compose.cpu.yml exec -T postgres \
  pg_dump -U monitor -d monitor -Fc \
  > "backups/postgres_$(date +%Y%m%d_%H%M%S).dump"
ls -lh backups
```

确认生成的 dump 文件大小不是 0。

### 14.2 证据与快照卷导出

证据与快照存放在 Docker volume `yolo_vlm_monitor_evidence_data`、`yolo_vlm_monitor_snapshot_data`。备份前先确认 volume 名：

```bash
docker volume ls | grep yolo_vlm_monitor
```

通过专用临时容器只读挂载后导出，并把备份写入 `/data/yolo_vlm_monitor/backups`：

```bash
cd /data/yolo_vlm_monitor
mkdir -p backups
docker run --rm \
  -v yolo_vlm_monitor_evidence_data:/evidence:ro \
  -v yolo_vlm_monitor_snapshot_data:/snapshots:ro \
  -v "$PWD/backups":/backups \
  alpine tar -czf /backups/evidence_snapshots_$(date +%Y%m%d_%H%M%S).tar.gz -C / evidence snapshots
ls -lh backups
```

不要直接删除或修改 `/data/docker_path/docker_data` 下的 volume 文件。数据库恢复和 volume 恢复会覆盖当前数据，应安排维护窗口并单独制定恢复步骤，不要在业务运行时直接操作。

## 15. 开发/持续更新模式（可选）

日常迭代时叠加开发覆盖层 `compose.cpu.dev.yml`，实现「改代码不重建镜像」：

```bash
cd /data/yolo_vlm_monitor
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml up -d
```

与生产基线相比，覆盖层追加：

- app 以 bind mount 挂载 `backend/`、`alembic/`、`run.py`，并设置 `APP_RELOAD=true`（uvicorn 热重载，后端改代码即时生效）。
- 新增 `web` 服务（node:22-bookworm-slim），挂载 `frontend/` 源码跑 Vite dev server（端口 `5173`，浏览器 HMR 即时更新），代理 `VITE_API_TARGET=http://app:8100`。
- 前端依赖装在独立卷 `web_node_modules`，避免宿主机 node_modules 覆盖/跨平台二进制冲突。

依赖变化（`requirements.txt`、`package.json`、`pnpm-lock.yaml`）仍需重建镜像。验证无误后，回到生产模式 `docker compose -f compose.cpu.yml up -d` 即可发布（前端代码随镜像重建包含最新 dist）。

## 16. 可选监控服务

启动 app、PostgreSQL、Redis，并额外启动 Prometheus/Grafana：

```bash
docker compose -f compose.cpu.yml --profile monitoring up -d
```

默认端口：

- Prometheus：`9090`
- Grafana：`3000`

启动前必须检查端口是否被占用：

```bash
ss -lntp | grep -E ':(3000|9090) ' || true
```

若已被老业务占用，不要启动 monitoring profile，先修改 Compose 端口映射。

## 17. 常见故障排查

### 17.1 8100 无法访问

```bash
docker compose -f compose.cpu.yml ps
docker compose -f compose.cpu.yml logs --tail=200 app
ss -lntp | grep ':8100 '
curl -v --max-time 10 http://127.0.0.1:8100/health
```

服务器内 curl 正常、Windows 访问失败时，检查网络路由和防火墙，或使用 SSH 隧道。不要为了排查直接关闭整机防火墙。

### 17.2 app 不断重启

```bash
docker compose -f compose.cpu.yml ps app
docker inspect "$(docker compose -f compose.cpu.yml ps -q app)" \
  --format 'Status={{.State.Status}} ExitCode={{.State.ExitCode}} Error={{.State.Error}}'
docker compose -f compose.cpu.yml logs --tail=200 app
```

重点检查数据库密码（`POSTGRES_PASSWORD` 与 postgres 初始化密码是否一致）、postgres 健康状态、模型文件、模型 SHA256 和端口冲突。

### 17.3 postgres 起不来或健康检查失败

```bash
docker compose -f compose.cpu.yml logs --tail=100 postgres
docker compose -f compose.cpu.yml exec -T postgres pg_isready -U monitor -d monitor
```

常见原因：

- `POSTGRES_PASSWORD` 未设置或与已有 postgres_data 卷的初始化密码不一致（见 §5 注意项）。
- 卷损坏或磁盘满：`df -h /data/docker_path/docker_data`。
- 首次初始化被中断：不要 `down -v` 删除卷（会丢数据），可备份后单独处理。

### 17.4 lzma Cannot allocate memory / can't start new thread

先执行 `free -h` 排除真实 OOM。如果内存充足，通常是旧 Docker seccomp 与 Debian 13 的兼容问题：

- 运行时：确认 app 的 Compose 配置包含 `security_opt: seccomp=unconfined`（§8）。
- 构建时：按 §8 的本地基础镜像兜底方案执行。

该设置降低 app 容器的 syscall 隔离，仅用于兼容当前旧环境，不应无条件复制到其他服务器。

### 17.5 pip 报 can't start new thread

确认 Dockerfile 的 pip 命令包含：

```text
--progress-bar off
```

### 17.6 意外开始下载 CUDA/CUDNN

立即按 `Ctrl+C` 终止当前 app 构建，然后确认先安装了 CPU wheel：

```dockerfile
RUN pip install --no-cache-dir --progress-bar off \
    --index-url https://download.pytorch.org/whl/cpu \
    torch==2.13.0+cpu torchvision==0.28.0+cpu
```

验证运行中的 app：

```bash
docker compose -f compose.cpu.yml exec -T app python -c \
  'import torch; print(torch.__version__); print(torch.cuda.is_available())'
```

预期版本包含 `+cpu`，并输出 `False`。

### 17.7 Ultralytics 配置目录 warning

日志可能提示 `/root/.config/Ultralytics` 不可写，并回退到 `/tmp/Ultralytics`。当前不影响服务健康，但容器重建后可能重新生成配置。如需固化，可在 compose 增加 `model_cache:/root/.cache` 之外的配置卷（可选）。

### 17.8 视频源已添加但一直离线

“添加成功”表示配置已经写入数据库，不代表服务器能够访问摄像头。先从服务器主机测试 RTSP 端口：

```bash
ip route get 10.152.167.33
timeout 6 bash -c '</dev/tcp/10.152.167.33/554' \
  && echo reachable || echo unreachable
```

再从 app 容器测试同一地址：

```bash
docker compose -f compose.cpu.yml exec -T app python -c \
  "import socket; socket.create_connection(('10.152.167.33', 554), 6); print('reachable')"
```

如果主机和容器都超时，需要网络管理员检查服务器到摄像头网段的路由、网关 ACL、防火墙和摄像头 554 端口；重复添加配置或重启 app 不能解决网络不通。正式服务器也不能直接读取 Windows 电脑上的本地文件，`file://` 必须指向容器内实际存在的文件。

### 17.9 外部大模型配置保存失败

- 公网 Base URL 必须使用 `https://`，例如 `https://example.com/v1`。
- 首次保存必须填写 API Key；以后留空表示保留已有密钥。
- “测试已保存配置”只测试数据库中已经保存的配置，应先保存再测试。
- 出现 401 通常表示密钥无效，404 通常表示 Base URL 或模型名不正确，超时表示服务器无法访问模型服务。

## 18. 每次发布后的验收清单

```bash
cd /data/yolo_vlm_monitor
docker compose -f compose.cpu.yml ps
docker compose -f compose.cpu.yml logs --tail=100 app
curl -sS --max-time 10 -w '\nHTTP_STATUS:%{http_code}\n' \
  http://127.0.0.1:8100/health
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

验收标准：

- app、postgres、redis 均为 `Up`，健康检查为 `healthy`。
- `/health` 返回 HTTP 200 且 `status` 为 `ok`。
- YOLO 与火烟检测器为 `ready`。
- `models/yolo26s.pt` 与烟火权重由 Git 随代码分发，并通过 Compose 的 `models/` 只读挂载到 app；重建容器时不会联网下载模型。
- 8100 只由本项目 app 占用。
- 原有老业务容器仍然运行。
- app 日志没有新的 traceback、连接失败或模型校验错误。
- 数据库为 PostgreSQL：`docker compose -f compose.cpu.yml exec -T postgres psql -U monitor -d monitor -c '\dt'` 能列出业务表。
