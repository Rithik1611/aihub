#!/usr/bin/env bash
# One shared Open Terminal sandbox for every chat (http://localhost:8070). Run from anywhere:
#   bash ai/apps/cablecore/sandbox/start-sandbox.sh
# Needs OPEN_TERMINAL_API_KEY and CC_DB_URL in cableAI/.env. CC_GATEWAY_URL is the backend's sandbox
# gateway as seen from the container (default: backend on the host, port 8000).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../../.." && pwd)"            # cableAI/
set -a; . "$ROOT/.env"; set +a
docker build -q -t cablecore-sandbox:latest "$HERE" >/dev/null
docker rm -f cablecore-sandbox >/dev/null 2>&1 || true
MSYS_NO_PATHCONV=1 docker run -d --name cablecore-sandbox --restart unless-stopped \
  -p 127.0.0.1:8070:8000 --add-host host.docker.internal:host-gateway \
  -e OPEN_TERMINAL_API_KEY -e CC_DB_URL \
  -e CC_API_BASE="${CC_GATEWAY_URL:-http://host.docker.internal:8000/api/cablecore/api}" \
  -e ZOEKT_URL=http://host.docker.internal:6070 \
  -v cablecore-sandbox-home:/home/user \
  -v "$(cd "$ROOT/../cableCORE_BE/prisma/models" && pwd -W):/home/user/cablecore/schema:ro" \
  -v "$(cd "$HERE" && pwd -W)/README.md:/home/user/cablecore/README.md:ro" \
  -v "$(cd "$HERE" && pwd -W)/cc.py:/home/user/cablecore/cc.py:ro" \
  -e PYTHONPATH=/home/user/cablecore \
  cablecore-sandbox:latest
