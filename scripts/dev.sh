#!/usr/bin/env bash
# Start y8y Todo in development mode (single local user, loopback bind,
# data in ./dev-data — isolated from any real deployment).
set -euo pipefail
cd "$(dirname "$0")/.."
export Y8Y_DEV_MODE=1
export Y8Y_PORT="${Y8Y_PORT:-8086}"
echo "Dev server: http://127.0.0.1:${Y8Y_PORT}/todo/  (data: ./dev-data)"
exec python3 src/serve.py
