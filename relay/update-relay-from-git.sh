#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/spoken-command-relay}"
SERVICE_NAME="${SERVICE_NAME:-spoken-command-relay}"
REF="${1:-main}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GIT_USER="${SUDO_USER:-}"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run this updater with sudo from a Git checkout:"
  echo "  sudo $0 ${REF}"
  exit 1
fi

if [[ ! -d "$REPO_DIR/.git" ]]; then
  echo "This updater must be run from inside the spoken-command-device Git checkout."
  exit 1
fi

if [[ ! -f "$APP_DIR/.env.local" ]]; then
  echo "$APP_DIR/.env.local was not found. Refusing to deploy without relay configuration."
  exit 1
fi

run_git() {
  if [[ -n "$GIT_USER" && "$GIT_USER" != "root" ]]; then
    sudo -H -u "$GIT_USER" git -C "$REPO_DIR" "$@"
  else
    git -C "$REPO_DIR" "$@"
  fi
}

run_git fetch --prune origin
run_git checkout "$REF"
run_git pull --ff-only origin "$REF"

install -m 0644 "$REPO_DIR/relay/server.py" "$APP_DIR/server.py"
install -m 0644 "$REPO_DIR/relay/.env.example" "$APP_DIR/.env.example"
install -m 0755 "$REPO_DIR/relay/install-relay-service.sh" "$APP_DIR/install-relay-service.sh"

python3 -m py_compile "$APP_DIR/server.py"

chown -R relay:relay "$APP_DIR"
chmod 750 "$APP_DIR"
chmod 640 "$APP_DIR/.env.local"

systemctl restart "$SERVICE_NAME"
sleep 1
systemctl --no-pager --full status "$SERVICE_NAME"
curl -fsS http://127.0.0.1:8080/health
echo
echo "Relay updated from ${REF} at $(run_git rev-parse --short HEAD)."
