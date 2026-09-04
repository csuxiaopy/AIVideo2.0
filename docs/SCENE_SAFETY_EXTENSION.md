# 营业厅场景与安全检测扩展

## 正式能力

| 场景 | 默认能力 | 普通排班 | 必需区域 |
| --- | --- | --- | --- |
| 员工工位 | 离岗、玩手机、黑屏 | 周一至周五 09:30–11:00、14:00–17:00 | 全屏岗位多边形 |
| 客户位/入口 | 人员计数、黑屏 | 每日 08:30–17:30 | 一条带方向统计线 |
| 库房/全局 | 烟火、区域入侵、黑屏 | 安全能力始终全天运行 | 一个可命名禁区 |

原有摄像头迁移为 `custom`，模式、区域、排班、历史分析和告警均不改变。在岗和人员吸烟保留为实验能力；工服、物品遗留、人员聚集、睡岗与通道堵塞仅注册为计划能力，不会进入调度队列。

## 检测链路

- 通用 YOLO：人员、手机候选、岗位占用、跨线计数和入侵人员。
- 独立烟火 YOLO：`fire/smoke` 两类，独立 critical 队列；火焰连续 2 帧、烟雾最近 5 帧至少 3 帧确认。
- 外部视觉模型：只复核玩手机与实验性人员吸烟；模型失败不产生业务告警。
- 黑屏：亮度、方差和近黑比例连续 3 次异常；与 RTSP 断流分开记录。

优先级为 `fire_smoke=critical`、`intrusion/black_screen=high`、`off_duty/phone_use=normal`、`people_flow=low`。普通队列与安全队列分离，同一摄像头通过进程内锁和 Redis 分布式锁保证最多一个在途任务。

## 烟火模型安全

开发试点权重放置于 `models/fire_smoke_yolov8.pt`，固定 SHA256 为：

`ac0a10257b2bc1f20c9d957f8adeeb61dd6140322fc19d0b4a116cb491776d16`

服务不会在启动时自动下载权重，哈希不一致时检测器进入故障状态。生产 GPU 环境必须提供审核后的 TensorRT `.engine` 与 `FIRE_SMOKE_ENGINE_SHA256`。该试点链路涉及 AGPL-3.0，闭源商业部署前必须完成模型及 Ultralytics 许可审查。

视觉烟火识别只是视频预警补充，不能替代认证的感烟、感温和消防报警设备。

## 新增接口

- `GET /api/scene-templates`
- `GET /api/capabilities`
- `GET/PUT /api/settings/detectors`
- `GET /api/runtime/workers`（分别返回通用与烟火 Worker/队列/检测器状态）

Webhook 在原有 HMAC-SHA256 签名基础上增加 `severity`、`scene_type`、`zone_name` 与 `fire_smoke_class`。
