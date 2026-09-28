#!/bin/bash
# Start the yourAICIV portal server.
# Usage: ./start.sh [port]        (default 8097; or set PORT)
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
export PORT="${1:-${PORT:-8097}}"

# Keep the birth settings across restarts: when a restart (the watchdog, a
# container restart) starts us without them in the env, take them from
# ~/.env, then $CIV_ROOT/.env. A value already in the env wins. The file is
# parsed, never sourced. portal_server.py does the same (env_file.py); this
# is the belt-and-braces copy. Keep the key list in step with PERSISTED_KEYS.
ENV_HOME="$HOME"
if [ -z "$ENV_HOME" ] || { [ "$ENV_HOME" = "/root" ] && [ -d /home/aiciv ]; }; then
  ENV_HOME="/home/aiciv"
fi

env_file_value() {  # env_file_value FILE KEY -> last value for KEY, unquoted
  local file="$1" key="$2" line value
  [ -r "$file" ] || return 0
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" "$file" 2>/dev/null | tail -n 1)" || true
  [ -n "$line" ] || return 0
  value="${line#*=}"
  value="${value#"${value%%[![:space:]]*}"}"      # trim leading space
  case "$value" in
    \"*) value="${value#\"}"; value="${value%%\"*}" ;;
    \'*) value="${value#\'}"; value="${value%%\'*}" ;;
    *)   value="$(printf '%s' "$value" | sed -E 's/[[:space:]]+#.*$//')" ;;
  esac
  value="${value%"${value##*[![:space:]]}"}"      # trim trailing space
  printf '%s' "$value"
}

for key in PORTAL_PUBLIC_URL TRIAL_CONFIG_PATH; do
  current="${!key}"
  [ -n "${current//[[:space:]]/}" ] && continue
  for file in "$ENV_HOME/.env" ${CIV_ROOT:+"$CIV_ROOT/.env"}; do
    value="$(env_file_value "$file" "$key")"
    if [ -n "$value" ]; then
      export "$key=$value"
      break
    fi
  done
done

if [ ! -f "$DIR/react-portal/dist/index.html" ]; then
  echo "[portal] react-portal/dist is missing. Build it first:"
  echo "         cd \"$DIR/react-portal\" && npm ci && npm run build"
  exit 1
fi

echo "[portal] Starting yourAICIV portal on port $PORT..."
exec python3 "$DIR/portal_server.py"
