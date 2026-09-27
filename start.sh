#!/bin/bash
# Start the yourAICIV portal server.
# Usage: ./start.sh [port]        (default 8097; or set PORT)
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
export PORT="${1:-${PORT:-8097}}"

if [ ! -f "$DIR/react-portal/dist/index.html" ]; then
  echo "[portal] react-portal/dist is missing. Build it first:"
  echo "         cd \"$DIR/react-portal\" && npm ci && npm run build"
  exit 1
fi

echo "[portal] Starting yourAICIV portal on port $PORT..."
exec python3 "$DIR/portal_server.py"
