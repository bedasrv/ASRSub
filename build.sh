#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "==> [1/5] Stopping old stack (containers + networks)"
docker compose down --remove-orphans

echo "==> [2/5] Removing old asrsub image"
docker rmi asrsub:latest 2>/dev/null || true

echo "==> [3/5] Pruning stale build cache"
docker builder prune -af

echo "==> [4/5] Building fresh image (--no-cache)"
docker compose build --no-cache

echo "==> [5/5] Starting stack"
docker compose up -d

echo
echo "==> Status"
docker compose ps
