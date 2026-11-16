#!/usr/bin/env bash
# Code search server for code_search / code_read and the sandbox's cc.code (http://127.0.0.1:6070, local only).
# Build or refresh the index first (picked up live, no restart):
#   uv run python -m ai.apps.cablecore.code [BE folder] [FE folder]
set -euo pipefail
INDEX="$(cd "$(dirname "$0")/../../.." && pwd -W)/.code_index"   # cableAI/.code_index
docker rm -f cablecore-zoekt >/dev/null 2>&1 || true
MSYS_NO_PATHCONV=1 docker run -d --name cablecore-zoekt --restart unless-stopped -p 127.0.0.1:6070:6070 \
  -v "$INDEX:/data/index:ro" --entrypoint zoekt-webserver ghcr.io/sourcegraph/zoekt:latest \
  -index /data/index -rpc -listen :6070
