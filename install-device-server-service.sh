#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="spoken-command-server"
SERVICE_DIR="/etc/sv/$SERVICE_NAME"
RUN_FILE="$SERVICE_DIR/run"
RUN_USER="$(stat -c '%U' "$ROOT_DIR")"
RUN_GROUP="$(stat -c '%G' "$ROOT_DIR")"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run this installer with sudo:"
  echo "  sudo $0"
  exit 1
fi

if [[ ! -d /etc/sv ]]; then
  echo "/etc/sv was not found. This installer expects runit, as used by this antiX setup."
  exit 1
fi

mkdir -p "$SERVICE_DIR"

cat > "$RUN_FILE" <<RUN
#!/usr/bin/env bash
set -euo pipefail

cd "$ROOT_DIR/server"

set -a
if [[ -f .env.local ]]; then
  . ./.env.local
fi
set +a

exec chpst -u "$RUN_USER:$RUN_GROUP" python3 -u server.py
RUN

chmod +x "$RUN_FILE"
chown -R root:root "$SERVICE_DIR"

if command -v update-service >/dev/null 2>&1; then
  update-service --add "$SERVICE_NAME" || true
elif [[ -d /var/service ]]; then
  ln -sfn "$SERVICE_DIR" "/var/service/$SERVICE_NAME"
elif [[ -d /service ]]; then
  ln -sfn "$SERVICE_DIR" "/service/$SERVICE_NAME"
else
  echo "Service files installed to $SERVICE_DIR."
  echo "Could not find update-service, /var/service, or /service to enable it automatically."
  echo "Enable it manually using this system's runit service manager."
  exit 1
fi

echo "Installed runit service: $SERVICE_NAME"
echo "Service user: $RUN_USER:$RUN_GROUP"
echo "Control it with:"
echo "  sudo sv status $SERVICE_NAME"
echo "  sudo sv restart $SERVICE_NAME"
echo "  sudo sv stop $SERVICE_NAME"
