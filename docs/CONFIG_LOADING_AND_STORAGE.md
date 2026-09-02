# 监衡（YOLO + VLM 监控平台）配置加载与存储机制

> 分析对象：当前工作区 `D:\project\yolo_vlm_monitor`（README 中的「监衡 / AIVideo2.0」）。
> 适用版本：后端 `backend/` 当前代码（FastAPI + SQLAlchemy + pydantic-settings）。
> 结论速览：**监控任务、告警规则、采集参数等业务配置全部存储在数据库**（Compose 下为 PostgreSQL 命名卷，裸跑下为 SQLite 文件），容器/进程重启后**保留**；**启动参数**（`.env` / 环境变量 / 代码默认值）每次启动重新加载进内存 `Settings` 对象；**运行时过程状态**（规则计数、调度表、队列、预览）纯内存，重启**重置**。

---

## 1. 启动时数据加载机制（dev 模式）

### 1.1 配置来源与加载链路

| 来源 | 载体 | 路径 | 是否进入容器 | 说明 |
| --- | --- | --- | --- | --- |
| 环境变量 | 容器进程环境 / 宿主机 shell | Compose `environment:` 注入；裸跑为系统环境变量 | 是 | 优先级最高，见 1.2 |
| 项目级 .env 文件 | 文本文件 | `D:\project\yolo_vlm_monitor\.env`（模板 `.env.example`） | **否**（Dockerfile 未 `COPY .env`，见下方说明） | pydantic-settings 的 `env_file` |
| 代码默认值 | `backend/config.py` | `Settings` 类字段默认值（L17–L42） | 是（编译进镜像） | 兜底 |

关键实现（`backend/config.py`）：

```python
ROOT = Path(__file__).resolve().parents[1]          # = D:\project\yolo_vlm_monitor
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )
    ...
@lru_cache
def get_settings() -> Settings: ...                  # 进程级单例，启动时加载一次
```

- 裸跑（`run.py`，`python run.py`）：`Settings` 读取项目根目录 `.env`（若存在）+ 环境变量 + 默认值。
- Compose 模式（`docker compose -f compose.cpu.yml up`）：镜像内**没有 `.env` 文件**（`Dockerfile.cpu` 只 COPY `backend/`、`alembic/`、`run.py` 等）。容器内 `Settings` 的取值全部来自 `compose.cpu.yml` 中 `environment:` 注入的变量（其值由宿主机 `.env` 经 `${VAR:-default}` 替换得到）或代码默认值。
- **宿主机 `.env` 承担两种角色**：① 供 Compose 文件做 `${VAR}` 变量替换（`POSTGRES_PASSWORD`、`APP_ENCRYPTION_KEY` 等，未设置会因 `POSTGRES_PASSWORD:?` 直接报错）；② 裸跑时供 pydantic 读取。两者路径相同（项目根目录），但机制不同。

### 1.2 加载优先级（全局 vs 项目级）

pydantic-settings 的覆盖顺序（高 → 低）：

```
① 环境变量（Compose environment / 容器注入 / 宿主机 shell 导出）   ← 全局级
② 项目根目录 .env 文件（env_file 指定）                            ← 项目级
③ Settings 类字段默认值（编译进代码）                              ← 编译时
```

运行时还有一层 **DB 覆盖**（动态配置优先于启动参数）：

- `MonitoringRuntime.__init__`（`backend/pipeline.py` L70–82）：检测器模型路径/设备取自 `detector_settings` 表，**表中值非空时覆盖** `Settings.yolo_model` / `fire_smoke_model` 等。
- `reload_models()`（L135–148）：VLM（玩手机/抽烟复核）的 Base URL / API Key / 模型名完全取自 `model_settings` 表，不走环境变量。
- 摄像头级配置（模式、几何、排班、参数、抽帧频率）全部取自 `cameras` 表，UI 修改即时生效。

### 1.3 启动时加载进内存、重启即重置的配置（内存态）

**A. `Settings` 对象（每次进程启动重新加载，属"配置重读"而非"丢失"）：**

字段全量清单（`backend/config.py` L17–42）：`app_host`、`app_port`、`app_reload`、`database_url`、`redis_url`、`app_encryption_key`、`evidence_dir`、`snapshot_dir`、`evidence_retention_days`、`max_live_previews`、`live_preview_fps`、`live_preview_timeout_seconds`、`frame_capture_timeout_seconds`、`yolo_model`、`yolo_device`、`yolo_imgsz`、`yolo_confidence`、`fire_smoke_model`、`fire_smoke_sha256`、`fire_smoke_device`、`fire_smoke_imgsz`、`scheduler_enabled`、`analysis_workers`、`fire_smoke_workers`、`web_dist_dir`。

**B. `MonitoringRuntime` 运行时过程状态（纯内存，重启后彻底丢失）：**

| 状态 | 位置 | 重启后 |
| --- | --- | --- |
| 规则状态：黑屏连续帧计数、离岗 `absence_since`、VLM 确认窗口 `positive_windows`、人流已计数 track ID、烟火连续帧 `fire_consecutive` / `smoke_window`、闯入跟踪集合、班次 `shift_started_at` | `backend/rules.py` `RuleStateRegistry` / `CameraRuleState` | 重置（如"黑屏连续 3 帧"从 0 重新累计） |
| 调度时间表 `next_run`、模式节流 `last_mode_run` | `backend/pipeline.py` L92–94 | 重置；启动时 `sync_cameras()` 按 DB 配置重建 |
| 已入队集合 `queued` / `fire_queued`、摄像头锁 `camera_locks` | `backend/pipeline.py` L95–97 | 清空 |
| 处理计数 `processed` / `failures`、`last_heartbeat` | `backend/pipeline.py` L98–100 | 归零 |
| 实时画面缓存 `sources` / `snapshots` / 预览会话 | `backend/media_capture.py`（`MediaGateway`） | 清空，需重新拉流 |
| 事件总线订阅者 | `backend/eventbus.py` | 清空 |

**C. 队列（取决于 Redis 是否可用，`backend/queueing.py`）：**

- Redis 可用（Compose 默认）：任务在 Redis Stream `monitor:tasks:{critical|high|normal|low}` 与 `monitor:fire-tasks:{critical|high|normal|low}`，Redis 配了 `--appendonly yes` + `redis_data` 卷，**重启保留**。
- Redis 不可用：回退 `asyncio.PriorityQueue`（纯内存），**重启丢失**。
- 摄像头分布式锁键 `monitor:camera-lock:{camera_id}`（TTL 120 秒），临时键，无持久化要求。

### 1.4 持久化到磁盘/数据库、重启保留的配置

| 类型 | 载体 | 完整路径 / 卷名 | 内容 |
| --- | --- | --- | --- |
| 业务数据库（Compose 生产） | PostgreSQL 17，库名 `monitor`，用户 `monitor` | 命名卷 `postgres_data`（容器内 `/var/lib/postgresql/data`） | 全部业务表（见 §2.2） |
| 业务数据库（本地裸跑 dev） | SQLite 文件 | `D:\project\yolo_vlm_monitor\data\yolo_vlm.db` | 同一套业务表 |
| 告警证据图 | 文件目录 | 裸跑 `data\evidence\`；Compose 卷 `evidence_data`（容器内 `/app/data/evidence`） | 告警可视化标注图（保留 30 天） |
| 场景快照 | 文件目录 | 裸跑 `data\snapshots\`；Compose 卷 `snapshot_data`（容器内 `/app/data/snapshots`） | 周期性快照 |
| 模型权重 | 只读挂载 | 裸跑 `models\fire_smoke_yolov8.pt`、`yolo26n.pt`；Compose `./models:/app/models:ro`、`./yolo26n.pt:/app/yolo26n.pt:ro` | YOLO 权重（含 SHA256 校验） |
| 模型缓存 | 卷 | `model_cache`（容器内 `/root/.cache`） | Ultralytics 等缓存 |
| 队列持久化 | Redis AOF | 命名卷 `redis_data`（容器内 `/data`） | Redis Stream 任务 |

### 1.5 启动加载时序

`run.py` → uvicorn 加载 `backend/main.py`，`lifespan`（L23–32）：

```
1. upgrade_schema()          # database.py：create_schema() 建缺表 + alembic upgrade head
2. context.runtime = MonitoringRuntime(context.settings, context.repository, context.cipher)
   ├─ 读 detector_settings 表 → 初始化 YOLO / 火烟检测器
   └─ 读 model_settings 表 → reload_models() 初始化 VLM
3. runtime.start()
   ├─ queue.start()          # 连接 Redis（失败则内存回退）
   ├─ reload_models()        # 从 model_settings 表加载 VLM
   ├─ media.start()
   └─ sync_cameras()         # 从 cameras 表读全部摄像头 → 解密 RTSP → 重建调度计划 next_run
4. 启动调度器、清理协程、N 个分析 worker（数量 = ANALYSIS_WORKERS / FIRE_SMOKE_WORKERS）
```

---

## 2. 监控模块（监控任务、告警规则、采集参数）存储位置

### 2.1 静态配置文件路径

**业务监控配置没有独立的 yaml/json 静态文件**（无 `monitoring.yaml` 之类）。项目内的静态文件只有：

| 文件 | 用途 |
| --- | --- |
| `D:\project\yolo_vlm_monitor\.env`（模板 `.env.example`） | 启动参数（数据库、Redis、YOLO、火烟、worker 数等） |
| `D:\project\yolo_vlm_monitor\compose.cpu.yml` | 生产基线编排：端口、环境变量注入、命名卷、健康检查、`restart: unless-stopped` |
| `D:\project\yolo_vlm_monitor\compose.cpu.dev.yml` | dev 覆盖层：backend/alembic/run.py bind mount + `APP_RELOAD=true` + Vite web 服务 |
| `D:\project\yolo_vlm_monitor\deploy\prometheus.yml` | Prometheus 抓取配置（仅 `--profile monitoring` 启动） |
| `D:\project\yolo_vlm_monitor\deploy\grafana\provisioning\datasources\prometheus.yml`、`...\dashboards\dashboard.yml` | Grafana 数据源与看板 provisioning |
| `D:\project\yolo_vlm_monitor\deploy\grafana\dashboards\monitor.json` | Grafana 看板 JSON |

编译进代码的静态定义（"编译时配置"）：`backend/capabilities.py` 的 `CORE_CAPABILITIES`（8 种检测模式）、`SCENE_TEMPLATES`（工位/客户位/安全区 3 个场景模板）、`backend/schemas.py` 的 `CameraOptions` 默认阈值、`FRAME_INTERVAL_CHOICES = {5, 10, 30, 60, 120}`。

### 2.2 运行时动态配置：数据库表

数据库位置二选一：**Compose** → PostgreSQL 库 `monitor`（卷 `postgres_data`）；**裸跑** → `data\yolo_vlm.db`（SQLite）。表结构由 `backend/models.py` 定义 + `alembic/versions/` 迁移。

**（1）监控任务 = `cameras` 表（每行一个摄像头/一路监控任务）**

| 列 | 存储内容 |
| --- | --- |
| `id` / `name` | 摄像头 ID / 名称 |
| `rtsp_url_encrypted` | RTSP 地址（`APP_ENCRYPTION_KEY` AES 加密存储） |
| `enabled` | 任务启停 |
| `scene_type` | 场景类型（workstation / customer_area / security_area / custom） |
| `modes_json` | 启用的检测模式列表（8 种：black_screen、off_duty、on_duty、people_flow、phone_use、smoking、fire_smoke、intrusion） |
| `geometry_json` | 岗位 ROI 多边形 / 人流统计线 / 入侵禁区多边形 |
| `schedule_json` | 排班（`timezone`、`weekly` 星期排班、`holidays` 节假日） |
| `options_json` | **采集参数 + 告警阈值**（`CameraOptions`，见下） |
| `frame_interval_seconds` | 抽帧频率（5/10/30/60/120 秒） |
| `online` / `last_seen_at` / `last_frame_at` / `last_analysis_at` / `last_error` | 运行时状态（每次心跳写库） |
| `created_at` / `updated_at` | 时间戳 |

`options_json` 全量字段（`backend/schemas.py` `CameraOptions`）：`health_interval_seconds=5`、`yolo_fps=0.1`、`behavior_interval_seconds=15`（兼容保留，玩手机/吸烟不再读取）、`off_duty_seconds=300`、`shift_grace_seconds=60`、`alert_cooldown_seconds=300`、`black_mean_max=18`、`black_std_max=12`、`black_ratio_min=0.92`、`fire_smoke_fps=1.0`、`fire_confidence=0.55`、`smoke_confidence=0.45`、`intrusion_confidence=0.50`、`intrusion_cooldown_seconds=60`。玩手机与吸烟在配置排班内共用固定 180 秒节流，以同一当前帧进行联合 VLM 检测。

**（2）告警规则 / 系统级设置（Webhook 为多行，其余设置为单行表）**

| 表 | 列 | 内容 |
| --- | --- | --- |
| `model_settings` | `provider`、`base_url`、`api_key_encrypted`、`economy_model`、`enhanced_model` | 视觉大模型（玩手机/抽烟复核）：Provider、Base URL、API Key（密文）、经济/增强模型 |
| `webhook_targets` | 名称、启用状态、URL、密钥密文、自动告警级别 | 多个 Webhook 外发目标；旧单条配置升级时自动迁入 |
| `webhook_deliveries` | 告警、目标快照、自动/手动、状态、错误 | 每条告警到每个目标的最新投递结果 |
| `detector_settings` | `general_model`、`general_device`、`fire_smoke_model`、`fire_smoke_device`、`model_sha256`、`license_name` | 检测器模型路径/设备/SHA256 |
| `retention_settings` | `alert_retention_days`、`auto_cleanup_enabled` | 告警保留天数与自动清理开关 |

**（3）业务记录表（运行产生的数据，非配置）**

| 表 | 内容 |
| --- | --- |
| `analyses` | 每次检测分析记录（模式、状态、置信度、原因、证据路径、VLM 用量/延迟） |
| `alerts` | 告警记录（severity、zone、webhook 状态、shadow 标记） |
| `traffic_aggregates` | 人流分钟聚合（current_count / entered / exited，唯一键 `(camera_id, bucket_start)`） |
| `alembic_version` | alembic 迁移版本号（自动维护） |

### 2.3 编译时配置 vs UI 界面修改的动态配置

| 维度 | 编译时 / 启动时配置 | UI 动态配置 |
| --- | --- | --- |
| 存储位置 | 代码默认值（`backend/config.py` `Settings`、`schemas.py` `CameraOptions`、`capabilities.py` 模板）+ 环境变量 + `.env` 文件 | PostgreSQL（卷 `postgres_data`）/ SQLite（`data\yolo_vlm.db`） |
| 对应表 | 无（不在数据库） | `cameras`、`model_settings`、`webhook_targets`、`webhook_deliveries`、`detector_settings`、`retention_settings` |
| 修改方式 | 编辑 `.env` / 改 Compose `environment:` / 改代码后重启 | 前端页面 → API（`POST/PATCH /api/cameras*`、`PUT /api/settings/*`） |
| 生效时机 | **重启进程/容器后生效**（`get_settings()` lru_cache 单例） | **立即生效**：`sync_cameras()` 重建调度、`reload_models()` 重载 VLM、`reload_detectors()` 重载检测器 |
| 重启后 | 每次启动重新读入内存 | 从数据库恢复，**不丢失** |
| 典型项 | `YOLO_IMGSZ`、`YOLO_CONFIDENCE`、`ANALYSIS_WORKERS`、`FRAME_CAPTURE_TIMEOUT_SECONDS`、端口、数据库地址、加密密钥 | 摄像头增删改、模式勾选、ROI/统计线/禁区绘制、排班、离岗阈值、告警冷却、模型配置、Webhook、保留天数 |

> 注意：UI 中“系统配置”保存 VLM、检测器和保留天数；独立“Webhook 管理”模块保存多个目标及告警级别。worker 数、抽帧超时等**只能**通过 `.env` / Compose 修改。

---

## 3. 排查建议

### 3.1 如何确认当前环境的监控配置在内存还是持久化存储

**① 查数据库（权威依据 —— 配置是否落库）**

```bash
# Compose（PostgreSQL）
docker compose -f compose.cpu.yml exec -T postgres psql -U monitor -d monitor -c '\dt'
docker compose -f compose.cpu.yml exec -T postgres psql -U monitor -d monitor \
  -c "SELECT id, name, enabled, frame_interval_seconds, modes_json, options_json FROM cameras;"
docker compose -f compose.cpu.yml exec -T postgres psql -U monitor -d monitor \
  -c "SELECT * FROM model_settings; SELECT * FROM webhook_targets; SELECT * FROM webhook_deliveries; SELECT * FROM detector_settings; SELECT * FROM retention_settings;"

# 裸跑（SQLite）
sqlite3 "D:\project\yolo_vlm_monitor\data\yolo_vlm.db" ".tables"
sqlite3 "D:\project\yolo_vlm_monitor\data\yolo_vlm.db" \
  "SELECT id, name, enabled, frame_interval_seconds FROM cameras;"
```

能查到行 → 配置已持久化，重启不丢；查不到 → 该配置未保存（或从未创建）。

**② 看运行时实际生效值（区分内存态与 DB 态）**

```bash
curl -s http://127.0.0.1:8100/health
# 关键字段：
#   queue.mode        = "redis"（队列持久化） 或 "in_memory"（内存回退，重启丢）
#   workers.processed / failures = 内存计数器，重启归零
#   detectors.*       = 当前检测器状态
curl -s http://127.0.0.1:8100/api/runtime/workers
```

**③ 对比配置来源**

```bash
# Compose 解析后的完整配置（变量替换结果）
docker compose -f compose.cpu.yml config
# 容器内实际环境变量
docker compose -f compose.cpu.yml exec -T app env | grep -E "YOLO|FIRE|SHADOW|APP_|DATABASE|REDIS"
# 裸跑：确认是否加载了 .env
ls -la "D:\project\yolo_vlm_monitor\.env"     # 当前工作区不存在（只有 .env.example）！
```

**④ 重启实验验证两类配置（最直接的判定）**

```bash
docker compose -f compose.cpu.yml restart app
# 重启后：processed/failures 归零（内存态），last_mode_run 节流重置；
# 而 GET /api/cameras、SELECT * FROM cameras 内容不变（DB 态）；
# GET /health 的 queue.mode 若为 in_memory，任务队列也归零。
```

**⑤ 查看容器挂载/卷归属**

```bash
docker inspect yolo_vlm_monitor_app_1 --format '{{json .Mounts}}'
docker volume ls | grep yolo_vlm_monitor
```

### 3.2 如何保证 docker compose 重启后监控配置不丢失

1. **正确启停，绝不删卷**
   - 允许：`docker compose -f compose.cpu.yml stop` → `start`、`restart app`、`up -d`。
   - **禁止：`docker compose down -v`**（删除全部命名卷 = 数据库、Redis、证据、快照全丢）。
   - 生产环境不得执行 `docker volume prune` / `docker system prune`。

2. **业务配置的持久化链路（Compose 已默认满足）**
   - 监控任务/告警规则/采集参数 → `cameras` 等表 → PostgreSQL → 卷 `postgres_data` ✔
   - 队列 → Redis AOF（`command: redis-server --appendonly yes`）→ 卷 `redis_data` ✔
   - 证据图 / 快照 → 卷 `evidence_data` / `snapshot_data` ✔
   - 模型权重 → `./models`、`./yolo26n.pt` 只读 bind mount ✔
   - 容器异常退出 → `restart: unless-stopped` 自动拉起 ✔

3. **`.env` 文件一致性（最容易踩的坑）**
   - 宿主机项目根目录 `.env` 必须存在（当前工作区只有 `.env.example`，直接 `up` 会因 `POSTGRES_PASSWORD:?` 报错）。
   - `POSTGRES_PASSWORD` 只在 **postgres_data 卷首次初始化**时生效；之后修改 `.env` 不会改已初始化的数据库密码，需同步改库内密码。
   - **`APP_ENCRYPTION_KEY` 必须保持不变**：更换密钥后，`cameras.rtsp_url_encrypted`、`model_settings.api_key_encrypted` 无法解密，需重新配置。`webhook_targets.secret_encrypted` 是旧版自定义 HMAC Webhook 的兼容字段，企业微信机器人不再读取它。

4. **dev 覆盖层（compose.cpu.dev.yml）注意**
   - bind mount 只覆盖 `backend/`、`alembic/`、`alembic.ini`、`run.py`、`frontend/`，**不挂载 `./data`**；Compose 下数据库始终是 PostgreSQL（`DATABASE_URL` 已注入），SQLite 分支（`data/yolo_vlm.db`）只在裸跑时使用。
   - 若要在 dev 容器里用 SQLite 且要持久化，需额外挂载 `./data:/app/data`。

5. **定期备份（含恢复演练）**
   - PostgreSQL 逻辑备份：`docker compose -f compose.cpu.yml exec -T postgres pg_dump -U monitor -d monitor -Fc > backups/postgres_$(date +%Y%m%d_%H%M%S).dump`
   - 证据/快照卷导出：临时容器只读挂载 `yolo_vlm_monitor_evidence_data`、`yolo_vlm_monitor_snapshot_data` 后 `tar` 打包（详见 `docs/CPU_DOCKER_OPS_GUIDE.md` §14）。

6. **重启后验收清单**
   ```bash
   docker compose -f compose.cpu.yml ps                        # 三服务 Up / healthy
   docker compose -f compose.cpu.yml exec -T postgres psql -U monitor -d monitor \
     -c "SELECT count(*) FROM cameras;"                        # 行数与重启前一致
   curl -s http://127.0.0.1:8100/health                        # status=ok, queue.mode=redis
   curl -s http://127.0.0.1:8100/api/cameras                   # 摄像头配置完整
   ```

---

## 4. 附：关键路径 / 表名速查

**文件与目录**

| 路径 | 内容 |
| --- | --- |
| `D:\project\yolo_vlm_monitor\.env` / `.env.example` | 启动参数（Compose 变量替换 + 裸跑 pydantic 读取） |
| `D:\project\yolo_vlm_monitor\backend\config.py` | `Settings`（pydantic-settings，`get_settings()` lru_cache 单例） |
| `D:\project\yolo_vlm_monitor\backend\database.py` | 引擎、`session_scope`、`upgrade_schema()`（create_all + alembic） |
| `D:\project\yolo_vlm_monitor\backend\models.py` | 全部 ORM 表定义 |
| `D:\project\yolo_vlm_monitor\backend\repository.py` | 读写持久层 |
| `D:\project\yolo_vlm_monitor\backend\pipeline.py` | `MonitoringRuntime`（内存运行时、调度、worker） |
| `D:\project\yolo_vlm_monitor\backend\rules.py` | `RuleStateRegistry`（纯内存规则状态） |
| `D:\project\yolo_vlm_monitor\backend\queueing.py` | Redis Stream 队列 / 内存回退 |
| `D:\project\yolo_vlm_monitor\backend\schemas.py` | `CameraOptions`（采集/告警参数默认值）、`ScheduleSpec`、`GeometrySpec` |
| `D:\project\yolo_vlm_monitor\backend\capabilities.py` | 8 种模式与 3 个场景模板（编译时静态） |
| `D:\project\yolo_vlm_monitor\data\yolo_vlm.db` | 裸跑 SQLite 数据库文件 |
| `D:\project\yolo_vlm_monitor\data\evidence\` | 告警证据图（裸跑） |
| `D:\project\yolo_vlm_monitor\data\snapshots\` | 场景快照（裸跑） |
| `D:\project\yolo_vlm_monitor\alembic\versions\` | 迁移：`20260805_01_scene_safety.py`、`20260826_02_snapshot_capture.py`、`20260828_03_alert_retention.py` |
| `D:\project\yolo_vlm_monitor\deploy\prometheus.yml`、`deploy\grafana\*` | 平台监控（Prometheus/Grafana）静态配置 |

**数据库表（Compose → 库 `monitor`；裸跑 → `data\yolo_vlm.db`）**

配置类：`cameras`、`model_settings`、`webhook_targets`、`detector_settings`、`retention_settings`；投递记录：`webhook_deliveries`
记录类：`analyses`、`alerts`、`traffic_aggregates`；迁移版本：`alembic_version`

**Redis 键（Compose）**

Stream：`monitor:tasks:{critical|high|normal|low}`、`monitor:fire-tasks:{critical|high|normal|low}`
锁：`monitor:camera-lock:{camera_id}`（TTL 120s）

**Docker 命名卷（Compose）**

`postgres_data`（业务配置+记录）、`redis_data`（队列）、`evidence_data`（证据图）、`snapshot_data`（快照）、`model_cache`（模型缓存）、`grafana_data`（Grafana，可选）、`web_node_modules`（dev 前端依赖）
