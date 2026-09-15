# 172.16.166.229 GPU 部署

部署目录：`/data/yolo_vlm_monitor`。使用 `compose.cpu.yml` 加 `compose.gpu.production.yml`，Compose 项目名固定为 `yolo_vlm_monitor`，保持已有数据卷和8100端口。

当前470驱动使用 PyTorch 2.7.1 + CUDA 11.8。镜像以服务器已备份的 `yolo-vlm-cpu-rollback:20260914` 为基础，保留已安装的 Ultralytics 8.4.143 和 Supervision 0.30.2。不要删除这个基础镜像。

## 修改代码后生效

将代码同步到上述目录后执行：

```bash
cd /data/yolo_vlm_monitor
bash scripts/update-gpu-production.sh
```

如果修改了前端：

```bash
bash scripts/update-gpu-production.sh --frontend
```

脚本构建新镜像、验证两张GPU、重建应用容器并等待健康检查。CPU/GPU依赖缓存不变时无需重新下载大包。仅修改Python文件后执行 `docker restart` 不会更新镜像内代码，必须运行构建脚本。

服务器包含本次启动修复，需先将这些变更合并回代码仓库；不要强制覆盖服务器文件。依赖文件变更需要同步调整生产 Dockerfile 的安装步骤。

## 查看状态

```bash
docker compose -p yolo_vlm_monitor -f compose.cpu.yml -f compose.gpu.production.yml ps
docker logs --tail 100 yolo_vlm_monitor-app-1
nvidia-smi
curl -fsS http://127.0.0.1:8100/health
```

数据库、旧镜像和部署配置备份位于 `/data/gpu-rollout-20260914`。不要执行 `down -v`。按用户确认继续使用现有主码流，不再以子码流为实施前提。

## 本次实测与限制

两张 T4 已分别完成 YOLO26m 和烟火模型的真实 CUDA 推理，应用亦已处理真实摄像头画面。143号（172.16.230.146:554）仍离线。改造前CPU解码占用约3240%，采集进程启动以来平均FPS约0.78～0.85。当前仍不是每路每秒完成所有模型分析的保证。

本次还修正了FFmpeg线程限制、Redis多流消息消费、公平调度、空子码流校验、重复字段迁移以及迁移后日志被禁用的问题。本地工作区与服务器均有这些未提交变更，请合并后再进行后续拉取。

## 主码流 NVDEC 解码（方案 A）

`compose.gpu.production.yml` 新增 `NVIDIA_DRIVER_CAPABILITIES=compute,utility,video`，并将 `CAPTURE_DECODE_DEVICES` 设为 `0,1`、`CAPTURE_GPU_STREAMS_PER_DEVICE` 设为 `32`。143号保留在 `CAPTURE_CPU_CAMERA_IDS`；64路按ID稳定排序分配，两卡各32路，其余18路保留CPU解码（含离线143号）。

64路阶段CPU实测约1355%，明显低于改造前约3240%；预热后采样窗口内GPU取帧无新增重连。尝试81路同时硬解时发生大量重连，已退回64路混合方案；尚未确定是并发初始化、资源竞争还是解码容量问题，不把该失败解释为T4固定只能解码32路。请勿未经压测直接提高该上限。

FFmpeg先使用NVDEC解码，在GPU帧上按1 FPS采样，然后只将选中帧下载到CPU，保持现有缩放和JPEG输出。当前硬件路径使用NV12（8位视频）；后续新增不同位深/格式的视频必须先验证，必要时将相应ID加入CPU列表。硬解失败不会自动把全部视频退回CPU，以防解码雪崩。

GPU采集连接按0.5秒错峰启动，并增加4个硬件帧缓冲。启动期需等待全部连接初始化，不应把最初几秒的帧率作为稳态结果。

最终版本初步采样：应用Docker CPU约1431%（约14.3个逻辑核），较改造前3240%降低约56%；64路GPU和17路CPU在线流均有新帧，GPU连续采样无新增重连。帧率和健康接口响应仍有波动，不能据此宣称已通过82路稳定1 FPS或全模型每秒分析验收。

按用户后续要求，`FIRE_SMOKE_MIN_INTERVAL_SECONDS=5` 限制自动烟火分析同一摄像头两次启动至少间隔5秒；不会降低取帧、通用模型的配置频率。该限制是频率上限，不保证繁忙时严格每5秒完成一次。手动立即分析不受自动调度间隔限制。

切换前另有镜像 `yolo-vlm-gpu:before-nvdec` 和 `/data/gpu-rollout-20260914/nvdec-before/` 配置及数据库备份。原CPU镜像不受影响。

仅修改解码分配配置时，不需要重新构建镜像，执行：

```bash
docker compose -p yolo_vlm_monitor -f compose.cpu.yml -f compose.gpu.production.yml up -d --no-deps --no-build app
```

若需退回CPU解码，将 `CAPTURE_GPU_STREAMS_PER_DEVICE` 改为 `"0"` 后执行上述命令；GPU模型分析仍保留。注意这会恢复原先较高的CPU负载。

观察实际解码利用率与滚动取帧指标：

```bash
nvidia-smi dmon -s u -c 5
docker exec -i yolo_vlm_monitor-app-1 python - < scripts/capture-performance.py
```

`/health` 每路采集状态包含 `decoder`、`decode_device`、`frames`、`reconnects`。显示配置为CUDA不等于成功取帧，需同时确认计数增长及 `nvidia-smi` 的 `dec` 利用率。

验证：45项采集/队列/安全相关测试通过；本地全量测试在既有 `test_annotation.py` 图像标签颜色断言处失败，未为此改动标注模块。分阶段生产日志见 `nvdec-stage4*`、`nvdec-stage64*`、`nvdec-stage81*`、`nvdec-final*`。短时采样不代替30分钟连续性能验收。
