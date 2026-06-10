#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/spoken-command-relay"
SERVICE_NAME="spoken-command-relay"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
RUN_USER="relay"
RUN_GROUP="relay"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run this installer with sudo:"
  echo "  sudo $0"
  exit 1
fi

if [[ ! -f "$APP_DIR/server.py" ]]; then
  echo "$APP_DIR/server.py was not found."
  echo "Deploy relay/server.py to $APP_DIR before installing the service."
  exit 1
fi

if [[ ! -f "$APP_DIR/.env.local" ]]; then
  echo "$APP_DIR/.env.local was not found."
  echo "Create it from .env.example and set relay tokens before installing the service."
  exit 1
fi

if ! getent group "$RUN_GROUP" >/dev/null 2>&1; then
  groupadd --system "$RUN_GROUP"
fi

if ! id "$RUN_USER" >/dev/null 2>&1; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin --gid "$RUN_GROUP" "$RUN_USER"
fi

chown -R "$RUN_USER:$RUN_GROUP" "$APP_DIR"
chmod 750 "$APP_DIR"

cat > "$SERVICE_FILE" <<UNIT
[Unit]
Description=Spoken Command Relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env.local
ExecStart=/usr/bin/python3 -u $APP_DIR/server.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"

echo "Installed systemd service: $SERVICE_NAME"
echo "Control it with:"
echo "  sudo systemctl status $SERVICE_NAME"
echo "  sudo systemctl restart $SERVICE_NAME"
