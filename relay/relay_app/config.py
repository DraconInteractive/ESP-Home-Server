"""Environment-derived configuration and process-wide shared state.

Every other module imports its settings from here so the environment is read
exactly once, at import time.
"""

from __future__ import annotations

import os
import threading
import time


HOST = os.environ.get("RELAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("RELAY_PORT", "8080"))

# Persisted relay state lives next to this package by default.
_RELAY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATABASE_PATH = os.environ.get(
    "RELAY_DATABASE_PATH",
    os.path.join(_RELAY_DIR, "relay-state.sqlite3"),
)
DEVICE_TOKENS_PATH = os.environ.get(
    "RELAY_DEVICE_TOKENS_PATH",
    os.path.join(_RELAY_DIR, "device-tokens.json"),
)
R1_UPDATE_MANIFEST_PATH = os.environ.get(
    "RELAY_R1_UPDATE_MANIFEST_PATH",
    os.path.join(_RELAY_DIR, "r1-update.json"),
)
R1_APK_DIR = os.environ.get(
    "RELAY_R1_APK_DIR",
    os.path.join(_RELAY_DIR, "r1-apk"),
)
STATIC_DIR = os.path.join(_RELAY_DIR, "static")

DEVICE_ENROLL_TOKEN = os.environ.get("RELAY_DEVICE_ENROLL_TOKEN", "")
SYNC_TOKEN = os.environ.get("RELAY_SYNC_TOKEN", "")
DASHBOARD_TOKEN = os.environ.get("RELAY_DASHBOARD_TOKEN", "")
ADMIN_TOKEN = os.environ.get("RELAY_ADMIN_TOKEN", DASHBOARD_TOKEN)
IP_PAIRING_TOKEN = os.environ.get("RELAY_IP_PAIRING_TOKEN", "")

NTFY_URL = os.environ.get("RELAY_NTFY_URL", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("RELAY_NTFY_TOPIC", "")
NTFY_TOKEN = os.environ.get("RELAY_NTFY_TOKEN", "")
NTFY_TITLE = os.environ.get("RELAY_NTFY_TITLE", "Dracon Relay")

DASHBOARD_CODE_TTL_SECONDS = int(os.environ.get("RELAY_DASHBOARD_CODE_TTL_SECONDS", "300"))
DASHBOARD_CODE_REQUEST_SECONDS = int(os.environ.get("RELAY_DASHBOARD_CODE_REQUEST_SECONDS", "60"))
DASHBOARD_SESSION_SECONDS = int(os.environ.get("RELAY_DASHBOARD_SESSION_SECONDS", str(12 * 60 * 60)))
EVENT_LEASE_SECONDS = int(os.environ.get("RELAY_EVENT_LEASE_SECONDS", "60"))
MAX_JSON_BYTES = int(os.environ.get("RELAY_MAX_JSON_BYTES", str(256 * 1024)))
RECENT_LIMIT = int(os.environ.get("RELAY_RECENT_LIMIT", "50"))
MAX_EVENT_ROWS = int(os.environ.get("RELAY_MAX_EVENT_ROWS", "50000"))

SERVER_STARTED_AT = int(time.time())

# Serialises all mutable state: SQLite writes and the in-memory dashboard
# code/session tables in ``auth``.
STATE_LOCK = threading.RLock()
