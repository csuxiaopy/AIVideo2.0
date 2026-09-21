# 异步后处理队列恢复手册

本流程默认不补发历史告警。禁止执行 `docker compose down -v`，禁止清空
`background_tasks`、`analyses` 或 `model_call_logs`。

## 1. 备份与只读检查

在 `/data/yolo_vlm_monitor` 执行：

```bash
mkdir -p /data/backups
docker compose -p yolo_vlm_monitor -f compose.cpu.yml -f compose.gpu.production.yml \
  exec -T postgres pg_dump -U monitor -d monitor -Fc \
  > /data/backups/monitor-before-postprocessing-recovery-$(date +%Y%m%d-%H%M%S).dump

curl -fsS http://127.0.0.1:8100/health
docker compose -p yolo_vlm_monitor -f compose.cpu.yml -f compose.gpu.production.yml ps
docker exec -i yolo_vlm_monitor-app-1 python scripts/recover-background-tasks.py
```

若脚本尚未进入当前镜像，可从宿主机项目目录通过标准输入执行只读检查：

```bash
docker exec -i -e PYTHONPATH=/app yolo_vlm_monitor-app-1 python - \
  < scripts/recover-background-tasks.py
```

数据库分类查询：

```sql
SELECT kind,status,error,count(*) AS count,min(created_at),max(updated_at)
FROM background_tasks
GROUP BY kind,status,error
ORDER BY kind,status,error;

SELECT id,camera_id,status,attempts,error,created_at,updated_at
FROM background_tasks
WHERE kind='alert_write' AND status='failed'
ORDER BY created_at;
```

## 2. 默认恢复：旧告警标记 stale

先预览，不带 `--apply`：

```bash
docker exec -i yolo_vlm_monitor-app-1 python scripts/recover-background-tasks.py \
  --mark-stale --before 2026-09-19T00:00:00+00:00 \
  --reason '2026-09-21 incident: historical event ended'
```

确认匹配范围后追加 `--apply`。该操作只把仍为 `failed` 的旧 `alert_write`
标记为已完成且结果为 stale，不创建 Alert、WebhookDelivery，也不会发送企业微信。
重复执行不会再次修改记录。

只有经人工确认事件仍有效时，才允许重试单个原任务：

```bash
docker exec -i yolo_vlm_monitor-app-1 python scripts/recover-background-tasks.py \
  --retry-task TASK_UUID --apply --allow-old-alert-delivery
```

此命令可能生成告警并发送旧 Webhook，不属于默认恢复流程。

## 3. 部署、验证与回滚

先在本地完成测试，再使用现有生产更新脚本构建和健康检查：

```bash
cd /data/yolo_vlm_monitor
bash scripts/update-gpu-production.sh
```

部署后每分钟检查 `/health`，持续 15～30 分钟。验收条件：

- `status` 为 `ok`；`postprocessing.degraded` 为 `false`；
- `writes.depth` 持续下降并远低于容量，`utilization < 0.5`；
- `writes.consecutive_failures=0`；
- `durable.last_analysis_write_at` 和 `last_model_log_write_at` 持续更新；
- `analysis_write`、`model_log_write` 的完成数量持续增加；
- Alert、AlertEvidence 和 WebhookDelivery 没有重复增长；
- 重启一次应用后仍能持续写入。

若验证失败，保留数据库和命名卷，使用部署前镜像标签重建 `app`，不要回滚或
删除数据库。代码回滚不会撤销已经标记 stale 的任务；其审计结果保留在
`result_json`，如确需恢复，应逐条人工评估，禁止批量改回 retry。
