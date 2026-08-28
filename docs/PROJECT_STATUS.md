# YOLO VLM 智能巡检项目情况说明

更新日期：2026-08-25  
本地项目：`D:\project\yolo_vlm_monitor`  
正式服务器：`172.16.166.229`  
服务器项目目录：`/data/yolo_vlm_monitor`

## 1. 项目用途

本项目用于接入营业厅、办公区和安全区域的视频源，通过本地 YOLO、烟火模型及可选的外部视觉大模型完成以下巡检能力：

- 摄像头在线状态和黑屏检测
- 人员在岗、离岗和玩手机检测
- 人流统计
- 区域入侵检测
- 烟火检测
- 外部视觉大模型复核
- 告警、证据和运行状态展示

项目由 Vue 前端、FastAPI 后端、PostgreSQL、Redis、FFmpeg、YOLO 和烟火检测模型组成。

## 2. 正式环境

| 项目 | 当前配置 |
| --- | --- |
| 操作系统 | CentOS 7 |
| Docker | 19.03.13 |
| Docker Compose | 1.29.2 |
| CPU | 2 × Intel Xeon Silver 4114 |
| CPU资源 | 20物理核、40线程 |
| 内存 | 62GB |
| GPU | 无 |
| 项目Compose | `compose.cpu.yml` |
| Web端口 | 8100 |
| Web地址 | `http://172.16.166.229:8100` |

服务器同时运行其他历史 Docker 业务。项目部署和维护必须限制在 `/data/yolo_vlm_monitor` 及其 Compose 资源范围内，不允许清理全局 Docker 资源、删除公共数据目录或重启 Docker daemon。

## 3. 当前部署状态

核心容器：

```text
yolo_vlm_monitor_app_1
yolo_vlm_monitor_postgres_1
yolo_vlm_monitor_redis_1
```

最近验收结果：

- app、PostgreSQL、Redis 均为 healthy。
- `/health` 返回 HTTP 200，状态为 `ok`。
- 通用 YOLO 状态为 ready，运行设备为 CPU。
- 烟火模型状态为 ready，运行设备为 CPU。
- Redis 普通和安全队列可用。
- 前端已经包含在 app 镜像中，通过8100端口提供。
- `yolo26n.pt` 已通过 Compose 只读挂载，重建容器时不再依赖 GitHub 下载。

## 4. 已完成的部署兼容处理

### 4.1 Debian软件源

由于服务器访问 `deb.debian.org` 不稳定，`Dockerfile.cpu` 已改用中国大陆可访问的软件源安装 FFmpeg 等系统依赖。

### 4.2 Python和Node基础镜像

`python:3.12-slim` 和 `node:22-alpine` 已通过可访问的镜像代理准备。Python依赖安装使用国内PyPI镜像，PyTorch使用CPU版本。

### 4.3 Windows前端依赖污染

`.dockerignore` 已排除 Windows 上传的 `frontend/node_modules`、构建输出和缓存，避免容器内出现 `vue-tsc: Permission denied`。

### 4.4 旧Docker兼容

当前 Docker 19.03 与新 Debian/glibc 存在 seccomp 兼容问题，app 服务使用：

```yaml
security_opt:
  - seccomp=unconfined
```

该设置只用于兼容当前服务器，不应直接复制到更新的生产环境。

## 5. 已修复问题

### 5.1 前端保存显示 `[object Object]`

前端现在能够解析 FastAPI 的结构化校验错误，并显示对应的中文字段和具体原因。

### 5.2 外部视觉大模型无法保存

生产环境要求公网模型地址使用 HTTPS。原配置使用：

```text
http://modelrouter.js96296.com/v1
```

已经确认该服务支持 HTTPS。前端现在会自动将公网 HTTP 地址升级为：

```text
https://modelrouter.js96296.com/v1
```

首次配置仍需重新填写 API Key。保存后留空代表继续使用已有密钥；“测试已保存配置”只测试已经保存到数据库的配置。

### 5.3 首页500错误

返回500的接口为：

```text
GET /api/dashboard
```

根因是 SQLite 和 PostgreSQL 的日期类型比较规则不同。原代码将 PostgreSQL `date` 与字符串比较，导致：

```text
operator does not exist: date = character varying
```

现已改为使用 UTC 当日起止时间范围查询。该修复没有修改数据库表结构、volume或业务数据。

### 5.4 YOLO重建后重新下载

项目已有的 `yolo26n.pt` 现在只读挂载到 `/app/yolo26n.pt`，避免容器重建后访问 GitHub 下载失败并进入 degraded 状态。

## 6. 当前视频源情况

系统曾成功保存3路摄像头配置，说明新增视频源接口和数据库写入正常。但服务器与app容器连接以下RTSP地址的554端口均超时：

```text
10.152.167.33:554
10.152.167.37:554
```

因此视频源显示离线的根因是正式服务器到摄像头网段不通，而不是后端未启动或添加功能失效。需要检查：

- 服务器到 `10.152.167.0/24` 的路由
- 中间网关ACL和防火墙
- 摄像头554端口
- 摄像头RTSP服务和账号权限
- 是否应使用摄像头子码流地址

正式服务器不能直接读取 Windows 电脑上的本地文件。`file://` 必须指向容器内实际存在并已挂载的文件。

## 7. 96路摄像头容量分析

这台服务器适合96路低频智能巡检，不适合96路同时进行秒级烟火、入侵实时检测。

单帧合成图实测：

| 模型 | 单帧热态耗时 | 理论单路吞吐 |
| --- | ---: | ---: |
| 通用YOLO | 约0.085秒 | 约11.8 FPS |
| 烟火模型 | 约0.068秒 | 约14.6 FPS |

该结果是合成空白帧测试，真实画面、并发推理、RTSP解码和服务器老业务会降低实际吞吐。建议本项目长期模型总负载控制在4～6 FPS。

如果96路全部每分钟检测一帧：

```text
96 ÷ 60 = 1.6 FPS
```

通用YOLO与烟火模型都每分钟一帧时，理论合计约3.2 FPS，模型推理部分可以支持。

## 8. 96路推荐方案

推荐常态运行策略：

| 功能 | 推荐值 |
| --- | --- |
| 后台基础抽帧 | 每路60秒1帧 |
| 通用YOLO | 每路60秒1帧 |
| 烟火模型 | 每路60秒1帧，或只对重点区域提高频率 |
| 黑屏检查 | 30～60秒一次 |
| 行为检测 | 60～120秒一次 |
| VLM复核 | 仅本地模型发现候选后调用 |
| 普通Worker | 2 |
| 烟火Worker | 1 |
| 首页展示 | 静态快照，不同时播放96路MJPEG |
| 人工实时预览 | 按需开启，最多同时1～4路，1～2 FPS |

每分钟一帧属于定期巡检，最坏告警延迟接近60秒，持续时间不足一分钟的事件可能完全漏检。如要求全部96路做到1～2秒内发现烟火或入侵，需要GPU、边缘设备事件触发或多节点分流。

## 9. 实时监控资源风险

媒体层已于2026-08-26改为周期单帧抓取。启用的视频源不再各自常驻FFmpeg；后台按照摄像头的 `frame_interval_seconds` 错峰启动短时抓帧进程，写入最近快照后立即退出。

当前首页只读取最近快照，实时预览必须由用户点击后按需启动，并受4路并发与60秒无心跳回收保护。`yolo_fps`、`fire_smoke_fps` 保留用于历史数据兼容，但后台周期由 `frame_interval_seconds` 统一控制。

已实现的保护包括：后台短时单帧抓取、首页静态快照、人工预览1～2 FPS、关闭即释放、断连与心跳超时回收。摄像头仍应优先使用640×360左右、300～800 Kbps的H.264子码流。

详细验收方法参见 `docs/SNAPSHOT_PREVIEW_OPS.md`。

## 10. 建议上线流程

不要一次性接入96路，建议逐级扩容：

1. 接入8路，连续观察2小时。
2. 增加到24路，观察半天。
3. 增加到48路，观察一天。
4. 确认队列和CPU稳定后增加到96路。

每阶段执行：

```bash
cd /data/yolo_vlm_monitor
docker stats --no-stream
curl -sS http://127.0.0.1:8100/health
curl -sS http://127.0.0.1:8100/api/runtime/workers
docker-compose -f compose.cpu.yml logs --tail=200 app
```

建议验收指标：

- app保持healthy。
- 通用和烟火检测器均为ready。
- 普通队列长期低于10。
- 烟火队列长期接近0。
- 队列不能连续3分钟增长。
- Worker失败数不能持续增加。
- app CPU尽量低于800%。
- 系统整体负载长期低于20～24。
- 内存无持续增长。

## 11. 常用运维命令

查看状态：

```bash
cd /data/yolo_vlm_monitor
docker-compose -f compose.cpu.yml ps
curl -sS http://127.0.0.1:8100/health
```

查看日志：

```bash
docker-compose -f compose.cpu.yml logs --tail=200 app
docker-compose -f compose.cpu.yml logs --tail=100 postgres
docker-compose -f compose.cpu.yml logs --tail=100 redis
```

重新构建和启动：

```bash
docker-compose -f compose.cpu.yml up -d --build
```

禁止执行：

```text
docker system prune
docker-compose down -v
删除 /data/docker_path/docker_data
删除不属于本项目的容器、镜像、网络或volume
未经确认升级或重启Docker daemon
```

更完整的部署和故障处理步骤参见 `docs/CPU_DOCKER_OPS_GUIDE.md`。
