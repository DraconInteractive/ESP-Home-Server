#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$ROOT_DIR/server"
PID_FILE="$SERVER_DIR/server.pid"
LOG_FILE="$SERVER_DIR/server.log"

cd "$SERVER_DIR"

stop_existing_server() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      echo "Stopping server pid $pid"
      kill "$pid"
      for _ in {1..30}; do
        if ! kill -0 "$pid" 2>/dev/null; then
          break
        fi
        sleep 0.2
      done
      if kill -0 "$pid" 2>/dev/null; then
        echo "Server pid $pid did not stop cleanly; forcing"
        kill -9 "$pid"
      fi
    fi
    rm -f "$PID_FILE"
  fi

  local pids
  pids="$(pgrep -f "python3( -u)? server.py" || true)"
  if [[ -n "$pids" ]]; then
    echo "Stopping existing server.py process(es): $pids"
    kill $pids 2>/dev/null || true
    sleep 1
  fi
}

start_server() {
  set -a
  if [[ -f .env.local ]]; then
    # shellcheck disable=SC1091
    . ./.env.local
  fi
  set +a

  echo "Starting device server on ${COMMAND_SERVER_HOST:-0.0.0.0}:${COMMAND_SERVER_PORT:-8080}"
  setsid python3 -u server.py </dev/null >> "$LOG_FILE" 2>&1 &
  local pid="$!"
  echo "$pid" > "$PID_FILE"
  sleep 1

  if ! kill -0 "$pid" 2>/dev/null; then
    echo "Server failed to start. Last log lines:"
    tail -40 "$LOG_FILE" || true
    exit 1
  fi

  echo "Server started: pid $pid"
  echo "Log: $LOG_FILE"
}

stop_existing_server
start_server
