#!/usr/bin/env bash
# Launch script for the analog-spec-tool: builds the frontend (if needed),
# then starts the single FastAPI process that serves both the built UI and
# the /api/* backend (single-origin mode - see backend/main.py's StaticFiles
# mount + SPA fallback, mounted after every /api route).
#
# Local use (unchanged from before this script existed):
#   ./run.sh
#
# Remote access over a private network (Tailscale-style tailnet, or any LAN):
#   ./run.sh --host 0.0.0.0            # bind every interface
#   ./run.sh --host 100.x.y.z          # bind just the tailnet IP
# Then open the printed URL (which already carries the access token) from
# any device on that network. The token itself lives only in
# ~/.config/analog-spec-tool/settings.json (or the ANALOG_SPEC_TOOL_TOKEN
# env var, which always wins and is never written to disk) - see
# backend/settings.py and backend/auth.py. A remote request without a valid
# token gets a plain 401; loopback (127.0.0.1/::1) requests skip the check
# entirely by default (backend/settings.py's loopback_exempt(), on by
# default, off-able by editing that settings file).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
DIST_DIR="$FRONTEND_DIR/dist"

HOST="127.0.0.1"
PORT="8000"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="$2"
      shift 2
      ;;
    --host=*)
      HOST="${1#*=}"
      shift
      ;;
    --port)
      PORT="$2"
      shift 2
      ;;
    --port=*)
      PORT="${1#*=}"
      shift
      ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "run.sh: unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

# Build the frontend if dist/ is missing, or older than the frontend source
# - so a code change is never silently served stale.
need_build=0
if [[ ! -f "$DIST_DIR/index.html" ]]; then
  need_build=1
elif find "$FRONTEND_DIR/src" "$FRONTEND_DIR/package.json" "$FRONTEND_DIR/vite.config.js" \
       -newer "$DIST_DIR/index.html" -print -quit 2>/dev/null | grep -q .; then
  need_build=1
fi

if [[ "$need_build" -eq 1 ]]; then
  echo "[run.sh] frontend/dist missing or stale - building (npm run build)..."
  (cd "$FRONTEND_DIR" && npm run build)
else
  echo "[run.sh] frontend/dist is up to date, skipping build"
fi

PY="$BACKEND_DIR/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "[run.sh] no backend/.venv found, falling back to system python3" >&2
  PY="python3"
fi

# Consumed by backend/main.py's startup event, purely to make its printed
# paste-and-go URL accurate for however this was launched.
export ANALOG_SPEC_TOOL_HOST="$HOST"
export ANALOG_SPEC_TOOL_PORT="$PORT"

echo "[run.sh] starting uvicorn on ${HOST}:${PORT} ..."
echo "[run.sh] (the access token + a ready-to-paste URL print below, once the app is up)"
cd "$BACKEND_DIR"
exec "$PY" -m uvicorn main:app --host "$HOST" --port "$PORT"
