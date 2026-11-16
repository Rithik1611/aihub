#!/usr/bin/env bash
# The Open Terminal sandbox (http://localhost:8070). Run from anywhere:
#   bash ai/apps/cablecore/sandbox/start-sandbox.sh
# Needs OPEN_TERMINAL_API_KEY in cableAI/.env. CC_GATEWAY_URL is the backend as seen from the container
# (default: backend on the host, port 8000); scripts read cableCORE data only through it, with a ticket.
#
# Multi-user: each chat user (X-User-Id) gets their own Linux account and home folder; other users get
# "Permission denied". The container holds no database login: cc.rows / cc.API go through the backend,
# which applies the user's licence and plants.
# ponytail: one container for everyone; a container per user when the sandbox must be a hard boundary.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../../.." && pwd)"            # cableAI/
set -a; . "$ROOT/.env"; set +a
docker build -q -t cablecore-sandbox:latest "$HERE" >/dev/null
docker rm -f cablecore-sandbox >/dev/null 2>&1 || true
GATEWAY="${CC_GATEWAY_URL:-http://host.docker.internal:8000/api/cablecore}"
MSYS_NO_PATHCONV=1 docker run -d --name cablecore-sandbox --restart unless-stopped \
  -p 127.0.0.1:8070:8000 --add-host host.docker.internal:host-gateway \
  -e OPEN_TERMINAL_API_KEY -e OPEN_TERMINAL_MULTI_USER=true \
  -e CC_API_BASE="$GATEWAY/api" -e CC_ROWS_URL="$GATEWAY/rows" \
  -e ZOEKT_URL=http://host.docker.internal:6070 \
  -v cablecore-sandbox-homes:/home \
  -v "$(cd "$ROOT/../cableCORE_BE/prisma/models" && pwd -W):/opt/cablecore/schema:ro" \
  -v "$(cd "$HERE" && pwd -W)/README.md:/opt/cablecore/README.md:ro" \
  -v "$(cd "$HERE" && pwd -W)/cc.py:/opt/cablecore/cc.py:ro" \
  -e PYTHONPATH=/opt/cablecore \
  cablecore-sandbox:latest
