# YOLO VLM Monitor CPU 版运维教程

本文适用于当前服务器环境：

- 服务器：`172.16.166.229`
- 系统：CentOS 7
- Docker：19.03.13
- docker-compose：1.29.2
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
docker-compose down -v
```

不要升级或重启 Docker daemon，除非已经评估对老业务的影响并获得明确确认。不要删除名称不以 `yolo_vlm_monitor` 开头的容器、镜像、网络或 volume。

## 2. 项目组成

默认执行 `docker-compose -f compose.cpu.yml up -d` 会运行：

| 服务 | 容器名 | 用途 | 宿主机端口 |
| --- | --- | --- | --- |
| app | `yolo_vlm_monitor_app_1` | 前端、API、YOLO 分析 | `8100` |
| postgres | `yolo_vlm_monitor_postgres_1` | 数据库 | 不对外暴露 |
| redis | `yolo_vlm_monitor_redis_1` | 队列和缓存 | 不对外暴露 |

前端已编译到 app 镜像的 `/app/frontend/dist`，不需要单独启动 Node.js 容器。访问 `/` 是前端页面，访问 `/health` 是健康接口。

Prometheus 和 Grafana 使用 `monitoring` profile，默认不会启动。

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
docker-compose --version
docker-compose -f compose.cpu.yml config >/dev/null
docker-compose -f compose.cpu.yml ps
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

Compose 支持从项目目录的 `.env` 读取变量。首次部署前建议创建 `.env`，至少修改密码和加密密钥：

```bash
cd /data/yolo_vlm_monitor
umask 077
cp -n .env.example .env
vi .env
```

重点配置：

```dotenv
POSTGRES_PASSWORD=请替换为强密码
APP_ENCRYPTION_KEY=请替换为足够长的随机密钥
YOLO_MODEL=yolo26n.pt
YOLO_DEVICE=cpu
FIRE_SMOKE_MODEL=models/fire_smoke_yolov8.pt
FIRE_SMOKE_DEVICE=cpu
ANALYSIS_WORKERS=2
FIRE_SMOKE_WORKERS=1
SHADOW_MODE=true
```

如果数据库已经使用旧密码初始化，不能只修改 `.env` 中的 `POSTGRES_PASSWORD`；还需在数据库中同步修改用户密码。否则 app 将无法连接数据库。

## 6. 检查模型文件

```bash
cd /data/yolo_vlm_monitor
ls -lh models
sha256sum models/fire_smoke_yolov8.pt
```

当前预期 SHA256：

```text
ac0a10257b2bc1f20c9d957f8adeeb61dd6140322fc19d0b4a116cb491776d16
```

`models` 目录以只读方式挂载到 app。不要在容器内修改该目录。

## 7. 首次部署：准备旧 Docker 兼容基础镜像

当前 `python:3.12-slim` 是 Debian 13。Docker 19 的默认 seccomp 与其线程创建存在兼容问题，直接执行 apt/dpkg 会出现：

```text
lzma error: Cannot allocate memory
RuntimeError: can't start new thread
```

这不代表宿主机真的缺少内存。当前项目使用本地基础镜像：

```text
yolo_vlm_monitor_python_ffmpeg:3.12-slim
```

先检查镜像是否存在：

```bash
docker image inspect yolo_vlm_monitor_python_ffmpeg:3.12-slim >/dev/null 2>&1
echo $?
```

返回 `0` 表示存在，可以跳过本节剩余步骤。

如果镜像不存在，确认临时容器名未被占用：

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

确认退出码为 `0`：

```bash
docker inspect yolo_vlm_monitor_cpu_base_builder \
  --format 'ExitCode={{.State.ExitCode}} Status={{.State.Status}}'
```

提交项目专用镜像：

```bash
docker commit --change 'CMD ["python3"]' \
  yolo_vlm_monitor_cpu_base_builder \
  yolo_vlm_monitor_python_ffmpeg:3.12-slim
```

只删除刚才由自己创建的临时容器：

```bash
docker rm yolo_vlm_monitor_cpu_base_builder
```

验证基础镜像：

```bash
docker run --rm yolo_vlm_monitor_python_ffmpeg:3.12-slim ffmpeg -version
```

不要给原始 `python:3.12-slim` 重新打覆盖性 tag，以免影响其他项目。

## 8. 构建 app 镜像

先备份 Dockerfile：

```bash
cd /data/yolo_vlm_monitor
cp -a Dockerfile.cpu "Dockerfile.cpu.bak.$(date +%Y%m%d_%H%M%S)"
```

构建：

```bash
docker-compose -f compose.cpu.yml build app
```

成功标志：

```text
Successfully tagged yolo_vlm_monitor_app:latest
```

Dockerfile 已进行以下兼容处理：

- Debian apt 使用阿里云镜像。
- pip 普通包使用阿里云 PyPI 镜像。
- PyTorch 从官方 CPU wheel 索引安装，不拉取 CUDA/CUDNN。
- pip 关闭 Rich 进度条，避免旧 seccomp 下创建刷新线程失败。

## 9. 启动项目

```bash
cd /data/yolo_vlm_monitor
docker-compose -f compose.cpu.yml up -d
```

这条命令会创建缺失容器或更新发生变化的项目容器，不会删除其他业务容器。

检查状态：

```bash
docker-compose -f compose.cpu.yml ps
```

预期三个服务均为 `Up`，并最终显示 `healthy`。

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
docker-compose -f compose.cpu.yml ps
```

启动全部默认服务：

```bash
docker-compose -f compose.cpu.yml up -d
```

只重启 app：

```bash
docker-compose -f compose.cpu.yml restart app
```

停止本项目容器但保留数据：

```bash
docker-compose -f compose.cpu.yml stop
```

重新启动已停止的项目容器：

```bash
docker-compose -f compose.cpu.yml start
```

不要使用 `down -v`，它会删除项目数据 volume。

## 12. 查看日志

app 最近 100 行：

```bash
docker-compose -f compose.cpu.yml logs --tail=100 app
```

持续跟踪 app：

```bash
docker-compose -f compose.cpu.yml logs -f --tail=100 app
```

查看所有项目服务：

```bash
docker-compose -f compose.cpu.yml logs --tail=100
```

退出实时日志按 `Ctrl+C`，不会停止容器。

## 13. 更新代码后的标准发布流程

```bash
cd /data/yolo_vlm_monitor
docker-compose -f compose.cpu.yml config >/dev/null
docker-compose -f compose.cpu.yml build app
docker-compose -f compose.cpu.yml up -d
docker-compose -f compose.cpu.yml ps
docker-compose -f compose.cpu.yml logs --tail=100 app
curl -sS --max-time 10 http://127.0.0.1:8100/health
```

如果只修改了 `compose.cpu.yml`，也应先运行 `config` 检查 YAML 和变量解析。

如果构建失败，不要立即清缓存或 prune。先保留完整错误信息：

```bash
docker-compose -f compose.cpu.yml build app 2>&1 | tee build-app.log
```

`build-app.log` 位于项目目录，便于定位最后一个失败步骤。

## 14. 数据备份

### 14.1 PostgreSQL 逻辑备份

```bash
cd /data/yolo_vlm_monitor
mkdir -p backups
docker exec yolo_vlm_monitor_postgres_1 \
  pg_dump -U monitor -d monitor -Fc \
  > "backups/postgres_$(date +%Y%m%d_%H%M%S).dump"
ls -lh backups
```

确认生成的 dump 文件大小不是 0。

### 14.2 证据文件备份

证据存放在 Docker volume `yolo_vlm_monitor_evidence_data`。备份前先确认 volume 名：

```bash
docker volume ls | grep yolo_vlm_monitor
```

不要直接删除或修改 `/data/docker_path/docker_data` 下的 volume 文件。建议通过专用临时容器只读挂载后导出，并把备份写入 `/data/yolo_vlm_monitor/backups`。

数据库恢复和 volume 恢复会覆盖当前数据，应安排维护窗口并单独制定恢复步骤，不要在业务运行时直接操作。

## 15. 可选监控服务

启动 app、PostgreSQL、Redis，并额外启动 Prometheus/Grafana：

```bash
docker-compose -f compose.cpu.yml --profile monitoring up -d
```

默认端口：

- Prometheus：`9090`
- Grafana：`3000`

启动前必须检查端口是否被占用：

```bash
ss -lntp | grep -E ':(3000|9090) ' || true
```

若已被老业务占用，不要启动 monitoring profile，先修改 Compose 端口映射。

## 16. 常见故障排查

### 16.1 8100 无法访问

```bash
docker-compose -f compose.cpu.yml ps
docker-compose -f compose.cpu.yml logs --tail=200 app
ss -lntp | grep ':8100 '
curl -v --max-time 10 http://127.0.0.1:8100/health
```

服务器内 curl 正常、Windows 访问失败时，检查网络路由和防火墙，或使用 SSH 隧道。不要为了排查直接关闭整机防火墙。

### 16.2 app 不断重启

```bash
docker inspect yolo_vlm_monitor_app_1 \
  --format 'Status={{.State.Status}} ExitCode={{.State.ExitCode}} Error={{.State.Error}}'
docker-compose -f compose.cpu.yml logs --tail=200 app
```

重点检查数据库密码、模型文件、模型 SHA256 和端口冲突。

### 16.3 apt 无法访问 deb.debian.org

确认 Dockerfile 包含：

```dockerfile
sed -i "s|http://deb.debian.org|http://mirrors.aliyun.com|g" /etc/apt/sources.list.d/debian.sources
```

### 16.4 lzma Cannot allocate memory

先执行 `free -h` 排除真实 OOM。如果内存充足，通常是旧 Docker seccomp 与 Debian 13 的兼容问题。确认项目专用基础镜像存在，并且 app 的 Compose 配置包含：

```yaml
security_opt:
  - seccomp=unconfined
```

该设置降低 app 容器的 syscall 隔离，仅用于兼容当前旧环境，不应无条件复制到其他服务器。

### 16.5 pip 报 can't start new thread

确认 pip 命令包含：

```text
--progress-bar off
```

### 16.6 意外开始下载 CUDA/CUDNN

立即按 `Ctrl+C` 终止当前 app 构建，然后确认先安装了 CPU wheel：

```dockerfile
RUN pip install --no-cache-dir --progress-bar off \
    --index-url https://download.pytorch.org/whl/cpu \
    torch==2.13.0+cpu torchvision==0.28.0+cpu
```

验证运行中的 app：

```bash
docker exec yolo_vlm_monitor_app_1 python -c \
  'import torch; print(torch.__version__); print(torch.cuda.is_available())'
```

预期版本包含 `+cpu`，并输出 `False`。

### 16.7 Ultralytics 配置目录 warning

日志可能提示 `/root/.config/Ultralytics` 不可写，并回退到 `/tmp/Ultralytics`。当前不影响服务健康，但容器重建后可能重新生成配置。

### 16.8 视频源已添加但一直离线

“添加成功”表示配置已经写入数据库，不代表服务器能够访问摄像头。先从服务器主机测试 RTSP 端口：

```bash
ip route get 10.152.167.33
timeout 6 bash -c '</dev/tcp/10.152.167.33/554' \
  && echo reachable || echo unreachable
```

再从 app 容器测试同一地址：

```bash
docker-compose -f compose.cpu.yml exec -T app python -c \
  "import socket; socket.create_connection(('10.152.167.33', 554), 6); print('reachable')"
```

如果主机和容器都超时，需要网络管理员检查服务器到摄像头网段的路由、网关 ACL、防火墙和摄像头 554 端口；重复添加配置或重启 app 不能解决网络不通。正式服务器也不能直接读取 Windows 电脑上的本地文件，`file://` 必须指向容器内实际存在的文件。

### 16.9 外部大模型配置保存失败

- 公网 Base URL 必须使用 `https://`，例如 `https://example.com/v1`。
- 首次保存必须填写 API Key；以后留空表示保留已有密钥。
- “测试已保存配置”只测试数据库中已经保存的配置，应先保存再测试。
- 出现 401 通常表示密钥无效，404 通常表示 Base URL 或模型名不正确，超时表示服务器无法访问模型服务。

## 17. 每次发布后的验收清单

```bash
cd /data/yolo_vlm_monitor
docker-compose -f compose.cpu.yml ps
docker-compose -f compose.cpu.yml logs --tail=100 app
curl -sS --max-time 10 -w '\nHTTP_STATUS:%{http_code}\n' \
  http://127.0.0.1:8100/health
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

验收标准：

- app、PostgreSQL、Redis 均为 `Up`，健康检查为 `healthy`。
- `/health` 返回 HTTP 200 且 `status` 为 `ok`。
- YOLO 与火烟检测器为 `ready`。
- `yolo26n.pt` 通过 Compose 只读挂载到 app，重建容器时不再访问 GitHub 下载。
- 8100 只由本项目 app 占用。
- 原有老业务容器仍然运行。
- app 日志没有新的 traceback、连接失败或模型校验错误。
