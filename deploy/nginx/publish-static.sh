#!/bin/sh
# Run after updating the app container; publish exactly its frontend build.
set -eu
container=${1:-yolo_vlm_monitor-app-1}
base=/www/wwwroot/aivideo
release="$base/releases/$(date +%Y%m%d-%H%M%S)-$$"
mkdir -p "$release"
docker cp "$container:/app/frontend/dist/." "$release/"
test -s "$release/index.html"
test -s "$release/brand-icon.png"
chmod -R a+rX "$release"
ln -s "$release" "$base/current.next"
mv -Tf "$base/current.next" "$base/current"
printf 'Published frontend: %s\n' "$release"
