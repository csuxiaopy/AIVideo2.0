#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
exec docker compose -f compose.cpu.yml -f compose.gpu.production.yml -f compose.split.yml "$@"
