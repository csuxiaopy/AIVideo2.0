# 实时检测解耦：实施状态

## 当前范围

2026-09-14 第二阶段已将自动检测中的大模型复核、告警、分析记录、模型日志及客流写入接入后台处理。
业务代码在 `backend/async_pipeline.py`，旧模式通过 `ASYNC_POSTPROCESSING=false` 保留。
生产 Compose 使用 `ASYNC_POSTPROCESSING=true`，复核并发 2、通知并发 4、I/O 线程 4、关键队列容量 4096。
模型、64 路 GPU / 18 路 CPU 解码分配、烟火最短 5 秒间隔和配置优先级未改变。
**处理链路解耦不等于 82 路分析 1 FPS 达标；最终频率必须看实际人员检测计数。**

第二阶段备份：`/data/postprocess-rollout-20260914`，回滚镜像 `yolo-vlm-gpu:before-postprocess-20260914`。
当前第二阶段镜像：`sha256:d7c3382ff813436100af2d3fd2c7f3b1591f262de8741af3bd24a802cc398896`。
2026-09-14 16:46:48（北京时间）启动，健康检查通过。
生产正常更新仍使用 `bash scripts/update-gpu-production.sh`。回滚仅恢复应用镜像和 Compose，
不要恢复旧数据库覆盖新产生的业务数据；新任务表保留，新模式再次启用后恢复未完成任务。

短时验证结果（不是连续 30 分钟验收）：

- 57 路在线离岗、43 路在线人流，多个 60 秒窗口人员检测中位数约 0.30～0.33 次/秒，约 3～3.3 秒一次；没有一路达到 0.95 次/秒。
- 81 路有画面，ID 143 仍离线；组批取帧年龄直方图在一次采样中 2882 / 2908 次不超过 1 秒。
- 初版逐条事务出现约 1200 条积压，已改为批量事务。旧任务恢复后，样本业务未完成任务保持个位数，写入失败 0、retained 0；未再出现持续增长。
- 复核仍有独立排队（最后样本 42 待处理、2 执行中），候选覆盖和过期审计生效。不能把复核排队描述为所有模型每秒完成一次。
- 最后采样：`/data/postprocess-rollout-20260914/performance-batch.jsonl`；部署日志 `deploy-batch.log`。

### 第一阶段历史基线

生产应用镜像：`sha256:2ce4e73e5eaf7bfec9a78ac520289f2616a4337e87533746a42981490db04b69`。
数据库迁移已到 `20260914_15`，双 GPU 容器运算检查及应用健康检查通过。
备份目录：`/data/async-capture-rollout-20260914`。
回滚镜像：`yolo-vlm-gpu:before-async-capture-20260914`。

上线短时检查：81 路有画面，已知离线 ID 143；64 路 GPU / 18 路 CPU 分配不变。
三个约 20 秒窗口中取帧 FPS 中位数 0.982、0.991、0.992，最新帧年龄 P95 小于 1 秒。
异步状态/快照失败数为 0，数据库 81 路最近帧时间在 10 秒内。
预热后的人员检测 60 秒窗口：57 路离岗摄像头有检测记录，频率中位数 0.117 次/秒，
最小 0.100、最大 0.133，无一路达到 0.95 次/秒。**不是 30 分钟验收，也不是分析 1 FPS 达标。**
生产性能采样文件：`/data/async-capture-rollout-20260914/performance.log`。

### 已实现

- `backend/background.py`：独立 I/O 线程池；4096 默认容量的关键写入入口；失败保留队首；普通状态最新值合并，默认 1 秒 / 100 项；复核队列默认并发 2，每个摄像头/类型一个执行中和一个最新待处理候选。
- 复核完成回调失败时保留结果并暂停该 key，支持只重试结果处理、不重做模型调用。该保留当前仅在内存，不能当作已持久化。
- 实际业务复核已接入事件、配置版本、排班、回岗、在线状态、证据时间和结果顺序校验；失效结果只记审计，不更新事件、不告警。
- `background_tasks` 表及增量迁移 `20260914_15`；幂等键冲突检查、租约及 fencing token、过期租约恢复、有限重试、手动重试方法。
- `TaskStore.complete()` / `complete_many()` 将业务写入和任务完成标记放在同一事务中。关键任务批量入库/执行最多 100 项，单条失败时回滚批次再逐条隔离。
- 客流当前值覆盖、增量累计，按原始分钟桶批量提交，事务重试不会重复累计。
- 告警证据先原子写入，告警与通知待办同事务，冷却以已提交告警时间为准；离岗仅提交成功后推进事件。
- 通知独立 4 worker，单次 HTTP 尝试，持久化外层最多 5 次尝试，失败任务支持管理员重试。
- GPU 组批派发时读取最新帧，返回与检测结果匹配的帧；不在检测 worker 内等待大模型、告警文件或业务数据库。
- 摄像头配置快照在配置保存后刷新，版本变化会使旧复核失效。
- 确定性目标文件名、同目录原子替换和 fsync 的证据写入函数。
- `CapturePersistence`：快照每路只保留最新待写帧；状态和最近分析时间合并；写入失败可见。启用后取帧发布不等待文件/数据库，实时取帧不加载旧磁盘快照。
- `monitor_person_detections_total`、`monitor_person_detection_last_timestamp_seconds` 及运行状态 `person_detection`：统计实际人员检测完成，不使用 `last_analysis_at` 代替。

### 开关

`ASYNC_CAPTURE_PERSISTENCE=false` 默认关闭，保留原处理路径。

`BACKGROUND_IO_WORKERS=4` 仅用于新增持久化 I/O 池，不调整 GPU worker。

`ASYNC_CAPTURE_PERSISTENCE` 只控制取帧持久化；`ASYNC_POSTPROCESSING` 启用第二阶段，并自动启用异步取帧持久化。
后续生产代码修改后使用 `bash scripts/update-gpu-production.sh` 重建并重启应用；
前端代码也变化时使用 `bash scripts/update-gpu-production.sh --frontend`。

### 尚未作为本轮完成项

1. 手动“立即分析”的独立任务 ID 和前端任务轮询尚未重做。当前手动操作仍等待本地检测，复核在后台进行。
2. 82 路连续 30 分钟、每路人员检测 ≥0.95 次/秒的完整验收未完成，不能声称稳定 1 FPS。
3. 摄像头锁仍覆盖 GPU 调用，但不覆盖大模型、文件写入、业务写库和自动通知。

### 测试记录

- 第二阶段最终本地回归：190 项通过；原标注文件除已知单人标注颜色断言外另 10 项通过，共 200 项。未把已知单人标注断言及两项 SQLite 清理/级联差异算作通过。

- `python -m pytest tests/test_background.py tests/test_capture_persistence.py tests/test_media_capture.py tests/test_queue_and_detector.py tests/test_schemas_security.py -q`：70 项通过（初次集成时）。
- 整体回归使用独立临时 SQLite，未使用生产数据库；排除原有 `test_annotation.py` 后 178 项中 2 项失败。失败为 `test_cleanup_tolerates_missing_evidence_file` 与 `test_camera_delete_cascades_traffic_in_postgresql_dev_database`。后者依赖 PostgreSQL 级联行为，不构成 PostgreSQL 已验证的证据。
- 新增业务测试包括：真实等待 30 秒的大模型、其他摄像头检测持续推进；回岗/禁用/配置改变/断流/结果乱序；客流增量幂等；证据写入失败不产生告警；队列满保留；目录成员再验证。
- 服务器独立 PostgreSQL 测试库 `monitor_async_test_20260914`、`monitor_async_test_batch_20260914` 验证客流幂等、告警去重、通知同事务、回滚、租约恢复、批量幂等；未发送外部通知。
- 管理员可查询 `GET /api/background-tasks/{task_id}`，失败任务通过 `POST /api/background-tasks/{task_id}/retry` 重试；复核审计保存失败可通过 `POST /api/cameras/{camera_id}/reviews/{kind}/retry-result` 重试保存，不重新调用模型。

### 耐久性边界

入内存队列不是保存成功。只有 `TaskStore.persist` 返回且事务提交，才属于重启可恢复任务。
关键入口关闭返回未持久化数量；调用者必须明确报告，不能当作安全清空。
外部通知响应丢失仍可能重投，不承诺外部 exactly-once。
迁移 downgrade 不删除任务表，避免回滚应用时丢失待处理工作和审计。
