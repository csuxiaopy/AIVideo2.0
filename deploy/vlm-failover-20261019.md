# 大模型自动替换上线记录

2026-10-19 上线。所有模型故障均须连续持续 30 分钟才能进入替换；任何有效响应清零，故障起点存入数据库，重启不会重新计时。鉴权、余额和账户限流只记录并退避，不以换模型处理。

协调器仅在分析服务运行。同一轮最多探测 5 个候选，每次请求 30 秒、整轮 180 秒。失败候选冷却 30 分钟，无可用候选 5 分钟后重试。候选须通过生成图片识别及实际业务 JSON 契约检查。配置与替换日志在同一事务提交，人工配置变更优先。后续模型切换无需重启视频采集或 GPU。

实际上线结果：qwen3.7-plus 持续返回渠道不可用；原有日志确认故障已超过 30 分钟。LightVision 不支持图片，qwen-vl 通过视觉及两项业务契约验证并自动采用。数据库替换日志 ID 1 记录成功；真实业务图片连续成功。公开站点设置、替换日志列表/详情/CSV 和实际模型测试均返回成功。

备份目录：服务器 /data/yolo_vlm_monitor/.deploy-backup-20261019-vlm-failover。
旧镜像：yolo-vlm-gpu:before-vlm-failover-20261019。
新镜像：yolo-vlm-gpu:vlm-failover-20261019（亦标记 production）。

回退程序时，将旧镜像重新标记 production，使用 deploy/split.sh 重建 app、web，再运行 deploy/nginx/publish-static.sh。保留当前可用模型配置与新增日志表，不执行数据库降级。旧源码位于备份 original/backend，旧前端源码在 frontend-source.tgz。回退前保留新增代码副本，勿覆盖新增审计记录。
