#!/usr/bin/env bash
set -euo pipefail
cd /data/yolo_vlm_monitor
compose=(docker compose -p yolo_vlm_monitor -f compose.cpu.yml -f compose.gpu.production.yml)
if [[ ${1:-} == --frontend ]]; then
  docker run --rm --security-opt seccomp=unconfined \
    -v "$PWD/frontend:/src" -w /src \
    -e COREPACK_NPM_REGISTRY=https://registry.npmmirror.com \
    -e npm_config_node_linker=hoisted \
    docker.m.daocloud.io/library/node:22-bookworm-slim \
    sh -lc 'corepack enable && pnpm install --frozen-lockfile && pnpm run build'
fi
"${compose[@]}" config --quiet
current=$(docker inspect --format '{{.Image}}' yolo_vlm_monitor-app-1)
docker tag "$current" yolo-vlm-gpu:previous
docker build -f Dockerfile.gpu.production -t yolo-vlm-gpu:production .
"${compose[@]}" run -T --rm --no-deps --entrypoint python app -c \
  'import torch; assert torch.cuda.device_count()==2; [torch.ones(1,device=f"cuda:{i}").sum().item() for i in range(2)]; print("Both GPUs passed")'
"${compose[@]}" up -d --no-deps --no-build app
for attempt in $(seq 1 30); do
  state=$(docker inspect --format '{{.State.Health.Status}}' yolo_vlm_monitor-app-1)
  if [[ "$state" == healthy ]]; then
    echo 'GPU application is healthy. Verify model activity with nvidia-smi.'
    exit 0
  fi
  sleep 5
done
echo 'Health check failed; restoring previous application image.' >&2
docker tag yolo-vlm-gpu:previous yolo-vlm-gpu:production
"${compose[@]}" up -d --no-deps --no-build app
exit 1
