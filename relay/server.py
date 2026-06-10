#!/usr/bin/env python3
"""Public relay for remote spoken-command devices.

The relay intentionally exposes a much smaller surface than the local command
server. Remote devices can register and enqueue events, while the home server
polls those events using an outbound authenticated connection.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


HOST = os.environ.get("RELAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("RELAY_PORT", "8080"))
DATABASE_PATH = os.environ.get(
    "RELAY_DATABASE_PATH",
    os.path.join(os.path.dirname(__file__), "relay-state.sqlite3"),
)
DEVICE_TOKENS_PATH = os.environ.get(
    "RELAY_DEVICE_TOKENS_PATH",
    os.path.join(os.path.dirname(__file__), "device-tokens.json"),
)
DEVICE_ENROLL_TOKEN = os.environ.get("RELAY_DEVICE_ENROLL_TOKEN", "")
SYNC_TOKEN = os.environ.get("RELAY_SYNC_TOKEN", "")
DASHBOARD_TOKEN = os.environ.get("RELAY_DASHBOARD_TOKEN", "")
ADMIN_TOKEN = os.environ.get("RELAY_ADMIN_TOKEN", DASHBOARD_TOKEN)
EVENT_LEASE_SECONDS = int(os.environ.get("RELAY_EVENT_LEASE_SECONDS", "60"))
MAX_JSON_BYTES = int(os.environ.get("RELAY_MAX_JSON_BYTES", str(256 * 1024)))
RECENT_LIMIT = int(os.environ.get("RELAY_RECENT_LIMIT", "50"))
SERVER_STARTED_AT = int(time.time())

STATE_LOCK = threading.RLock()


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    status: int
    message: str


def db_connect() -> sqlite3.Connection:
    directory = os.path.dirname(DATABASE_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_database() -> None:
    with db_connect() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                remote_addr TEXT,
                user_agent TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                received_at INTEGER NOT NULL,
                device_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                delivered_at INTEGER,
                acked_at INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                ack_ok INTEGER,
                ack_error TEXT
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_pending
            ON events(acked_at, delivered_at, received_at)
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_snapshots (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                received_at INTEGER NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
        """)


def clean_id(value: str, fallback: str = "") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", value.strip())
    return cleaned[:80] or fallback


def token_from_header(handler: BaseHTTPRequestHandler) -> str:
    value = handler.headers.get("Authorization", "").strip()
    prefix = "Bearer "
    if value.startswith(prefix):
        return value[len(prefix):].strip()
    return ""


def token_matches(expected: str, provided: str) -> bool:
    return bool(expected) and hmac.compare_digest(expected, provided)


def require_static_token(handler: BaseHTTPRequestHandler, expected: str, role: str) -> AuthResult:
    if not expected:
        return AuthResult(False, HTTPStatus.SERVICE_UNAVAILABLE, f"{role} token is not configured")
    if token_matches(expected, token_from_header(handler)):
        return AuthResult(True, HTTPStatus.OK, "")
    return AuthResult(False, HTTPStatus.UNAUTHORIZED, "unauthorized")


def load_device_tokens() -> dict[str, str]:
    try:
        with open(DEVICE_TOKENS_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}

    if isinstance(payload, dict) and isinstance(payload.get("devices"), dict):
        payload = payload["devices"]
    if not isinstance(payload, dict):
        return {}

    result: dict[str, str] = {}
    for raw_id, raw_secret in payload.items():
        device_id = clean_id(str(raw_id))
        if isinstance(raw_secret, dict):
            secret = str(raw_secret.get("secret", ""))
        else:
            secret = str(raw_secret)
        if device_id and secret:
            result[device_id] = secret
    return result


def save_device_tokens(tokens: dict[str, str]) -> None:
    directory = os.path.dirname(DEVICE_TOKENS_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = {"devices": {device_id: tokens[device_id] for device_id in sorted(tokens)}}
    temp_path = f"{DEVICE_TOKENS_PATH}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    os.replace(temp_path, DEVICE_TOKENS_PATH)


def generate_device_token() -> str:
    return secrets.token_urlsafe(32)


def require_device_token(handler: BaseHTTPRequestHandler, device_id: str) -> AuthResult:
    tokens = load_device_tokens()
    expected = tokens.get(device_id)
    if not expected:
        return AuthResult(False, HTTPStatus.FORBIDDEN, "device token is not configured")
    if token_matches(expected, token_from_header(handler)):
        return AuthResult(True, HTTPStatus.OK, "")
    return AuthResult(False, HTTPStatus.UNAUTHORIZED, "unauthorized")


def authorize_registration(handler: BaseHTTPRequestHandler, device_id: str) -> tuple[AuthResult, str | None]:
    provided = token_from_header(handler)
    tokens = load_device_tokens()
    existing = tokens.get(device_id)
    if existing:
        if token_matches(existing, provided):
            return AuthResult(True, HTTPStatus.OK, ""), None
        if token_matches(DEVICE_ENROLL_TOKEN, provided):
            return AuthResult(True, HTTPStatus.OK, ""), None
        return AuthResult(False, HTTPStatus.UNAUTHORIZED, "unauthorized"), None

    if not DEVICE_ENROLL_TOKEN:
        return AuthResult(False, HTTPStatus.SERVICE_UNAVAILABLE, "device enrollment token is not configured"), None
    if not token_matches(DEVICE_ENROLL_TOKEN, provided):
        return AuthResult(False, HTTPStatus.UNAUTHORIZED, "unauthorized"), None

    device_secret = generate_device_token()
    tokens[device_id] = device_secret
    save_device_tokens(tokens)
    return AuthResult(True, HTTPStatus.OK, ""), device_secret


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def html_response(handler: BaseHTTPRequestHandler, status: int, body: str) -> None:
    encoded = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(encoded)


def auth_error(handler: BaseHTTPRequestHandler, result: AuthResult) -> None:
    json_response(handler, result.status, {"ok": False, "error": result.message})


def read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length_text = handler.headers.get("Content-Length", "")
    if not length_text:
        return {}
    try:
        length = int(length_text)
    except ValueError as exc:
        raise ValueError("invalid content length") from exc
    if length > MAX_JSON_BYTES:
        raise ValueError("request body too large")
    body = handler.rfile.read(length)
    if not body:
        return {}
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


def client_ip(handler: BaseHTTPRequestHandler) -> str:
    forwarded = handler.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
    return forwarded or handler.client_address[0]


def public_device(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    try:
        status = json.loads(row["status_json"] or "{}")
    except json.JSONDecodeError:
        status = {}
    if not isinstance(payload, dict):
        payload = {}
    if not isinstance(status, dict):
        status = {}

    device = {
        "id": row["device_id"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "remote_addr": row["remote_addr"],
        "user_agent": row["user_agent"],
        "status": status,
    }
    for key in ("type", "model", "device_type", "firmware", "firmware_version", "firmware_project", "capabilities"):
        if key in payload:
            device[key] = payload[key]
    return device


def row_event(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "id": row["id"],
        "received_at": row["received_at"],
        "device_id": row["device_id"],
        "event_type": row["event_type"],
        "payload": payload,
        "delivered_at": row["delivered_at"],
        "acked_at": row["acked_at"],
        "attempts": row["attempts"],
        "ack_ok": None if row["ack_ok"] is None else bool(row["ack_ok"]),
        "ack_error": row["ack_error"] or "",
    }


def upsert_device(device_id: str, payload: dict[str, Any], handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    now = int(time.time())
    with db_connect() as connection:
        existing = connection.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        existing_payload: dict[str, Any] = {}
        existing_status: dict[str, Any] = {}
        if existing:
            try:
                loaded_payload = json.loads(existing["payload_json"] or "{}")
                if isinstance(loaded_payload, dict):
                    existing_payload = loaded_payload
            except json.JSONDecodeError:
                existing_payload = {}
            try:
                loaded_status = json.loads(existing["status_json"] or "{}")
                if isinstance(loaded_status, dict):
                    existing_status = loaded_status
            except json.JSONDecodeError:
                existing_status = {}

        status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
        merged_payload = dict(existing_payload)
        merged_payload.update(payload)
        merged_status = dict(existing_status)
        merged_status.update(status)
        if merged_status:
            merged_payload["status"] = merged_status

        connection.execute(
            """
            INSERT INTO devices (
                device_id, first_seen, last_seen, remote_addr, user_agent,
                payload_json, status_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                remote_addr = excluded.remote_addr,
                user_agent = excluded.user_agent,
                payload_json = excluded.payload_json,
                status_json = excluded.status_json
            """,
            (
                device_id,
                now,
                now,
                client_ip(handler),
                handler.headers.get("User-Agent", ""),
                json.dumps(merged_payload, separators=(",", ":")),
                json.dumps(merged_status, separators=(",", ":")),
            ),
        )
        row = connection.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
    return public_device(row)


def enqueue_event(device_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    event_id = uuid.uuid4().hex
    now = int(time.time())
    with db_connect() as connection:
        connection.execute(
            """
            INSERT INTO events (id, received_at, device_id, event_type, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_id, now, device_id, event_type, json.dumps(payload, separators=(",", ":"))),
        )
        row = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return row_event(row)


def queue_register_event(device_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return enqueue_event(device_id, "register", payload)


def queue_button_event(device_id: str, payload: dict[str, Any], handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    event = {
        "event": str(payload.get("event", "click"))[:32],
        "button": str(payload.get("button", "button"))[:32],
        "gpio": payload.get("gpio"),
        "active_low": payload.get("active_low"),
        "click_count": payload.get("click_count"),
        "uptime_ms": payload.get("uptime_ms"),
        "remote_addr": client_ip(handler),
    }
    return enqueue_event(device_id, "button", event)


def pending_events(limit: int) -> list[dict[str, Any]]:
    now = int(time.time())
    lease_cutoff = now - EVENT_LEASE_SECONDS
    with db_connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM events
            WHERE acked_at IS NULL
              AND (delivered_at IS NULL OR delivered_at < ?)
            ORDER BY received_at ASC
            LIMIT ?
            """,
            (lease_cutoff, limit),
        ).fetchall()
        ids = [row["id"] for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            connection.execute(
                f"""
                UPDATE events
                SET delivered_at = ?, attempts = attempts + 1
                WHERE id IN ({placeholders})
                """,
                (now, *ids),
            )
            rows = connection.execute(
                f"SELECT * FROM events WHERE id IN ({placeholders}) ORDER BY received_at ASC",
                ids,
            ).fetchall()
    return [row_event(row) for row in rows]


def ack_event(event_id: str, payload: dict[str, Any]) -> bool:
    now = int(time.time())
    ok = bool(payload.get("ok", True))
    error = str(payload.get("error", ""))[:240]
    with db_connect() as connection:
        cursor = connection.execute(
            """
            UPDATE events
            SET acked_at = ?, ack_ok = ?, ack_error = ?
            WHERE id = ?
            """,
            (now, 1 if ok else 0, error, event_id),
        )
        return cursor.rowcount > 0


def store_dashboard_snapshot(payload: dict[str, Any]) -> None:
    now = int(time.time())
    with db_connect() as connection:
        connection.execute(
            """
            INSERT INTO dashboard_snapshots (id, received_at, payload_json)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                received_at = excluded.received_at,
                payload_json = excluded.payload_json
            """,
            (now, json.dumps(payload, separators=(",", ":"))),
        )


def latest_dashboard_snapshot() -> dict[str, Any] | None:
    with db_connect() as connection:
        row = connection.execute("SELECT * FROM dashboard_snapshots WHERE id = 1").fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {"received_at": row["received_at"], "payload": payload}


def relay_snapshot() -> dict[str, Any]:
    with db_connect() as connection:
        device_rows = connection.execute("SELECT * FROM devices ORDER BY device_id ASC").fetchall()
        recent_rows = connection.execute(
            "SELECT * FROM events ORDER BY received_at DESC LIMIT ?",
            (RECENT_LIMIT,),
        ).fetchall()
        pending_count = connection.execute(
            "SELECT count(*) AS count FROM events WHERE acked_at IS NULL",
        ).fetchone()["count"]
        acked_count = connection.execute(
            "SELECT count(*) AS count FROM events WHERE acked_at IS NOT NULL",
        ).fetchone()["count"]
    devices = [public_device(row) for row in device_rows]
    recent_events = [row_event(row) for row in reversed(recent_rows)]
    home = latest_dashboard_snapshot()
    return {
        "relay": {
            "host": HOST,
            "port": PORT,
            "started_at": SERVER_STARTED_AT,
            "uptime_seconds": int(time.time() - SERVER_STARTED_AT),
            "device_count": len(devices),
            "pending_event_count": pending_count,
            "acked_event_count": acked_count,
            "has_sync_token": bool(SYNC_TOKEN),
            "has_dashboard_token": bool(DASHBOARD_TOKEN),
        },
        "devices": devices,
        "recent_events": recent_events,
        "home": home,
    }


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dracon Relay</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, -apple-system, Segoe UI, sans-serif; }
    body { margin: 0; background: #f7f7f4; color: #1f2933; }
    header { border-bottom: 1px solid #d7d7d2; background: #ffffff; padding: 18px 24px; }
    main { max-width: 1100px; margin: 0 auto; padding: 22px; }
    h1 { margin: 0; font-size: 1.35rem; }
    h2 { margin: 24px 0 10px; font-size: 1rem; }
    .muted { color: #667085; font-size: .9rem; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }
    .stat, .row { border: 1px solid #d7d7d2; border-radius: 8px; background: #fff; padding: 12px; }
    .stat strong { display: block; font-size: 1.6rem; }
    .rows { display: grid; gap: 8px; }
    details.panel { margin-top: 24px; }
    details.panel > summary { cursor: pointer; font-weight: 700; list-style-position: outside; }
    details.panel > summary h2 { display: inline; margin-left: 4px; }
    .device-meta { display: grid; gap: 3px; margin-top: 8px; }
    pre { margin: 10px 0 0; overflow: auto; white-space: pre-wrap; word-break: break-word; font-size: .82rem; }
    code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
    button, input { font: inherit; }
    input { min-width: 260px; padding: 8px; }
    button { padding: 8px 12px; }
    @media (prefers-color-scheme: dark) {
      body { background: #171a1f; color: #e5e7eb; }
      header, .stat, .row { background: #20242b; border-color: #343a45; }
      .muted { color: #aab2c0; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Dracon Relay</h1>
    <div class="muted">Read-only public relay dashboard</div>
  </header>
  <main>
    <form id="auth" hidden>
      <input id="token" type="password" autocomplete="current-password" placeholder="Dashboard token">
      <button type="submit">Save</button>
    </form>
    <div id="status" class="muted">Loading...</div>
    <section class="grid" id="summary"></section>
    <h2>Remote Devices</h2>
    <section class="rows" id="devices"></section>
    <details class="panel" id="eventsPanel" open>
      <summary><h2>Recent Relay Events</h2></summary>
      <section class="rows" id="events"></section>
    </details>
    <h2>Home Snapshot</h2>
    <section class="rows" id="home"></section>
    <h2>Home Devices</h2>
    <section class="rows" id="homeDevices"></section>
  </main>
  <script>
    const tokenKey = "draconRelayDashboardToken";
    const statusEl = document.getElementById("status");
    const auth = document.getElementById("auth");
    const token = document.getElementById("token");

    function el(tag, className, text) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined) node.textContent = text;
      return node;
    }
    function clear(node) {
      while (node.firstChild) node.removeChild(node.firstChild);
    }
    function timeText(value) {
      if (!value) return "";
      return new Date(value * 1000).toLocaleString();
    }
    function jsonBlock(value) {
      const block = el("pre", "");
      block.textContent = JSON.stringify(value, null, 2);
      return block;
    }
    function authHeaders() {
      const saved = localStorage.getItem(tokenKey) || "";
      return saved ? {Authorization: `Bearer ${saved}`} : {};
    }
    auth.addEventListener("submit", (event) => {
      event.preventDefault();
      localStorage.setItem(tokenKey, token.value);
      token.value = "";
      load();
    });
    function render(data) {
      statusEl.textContent = `Updated ${new Date().toLocaleTimeString()}`;
      const summary = document.getElementById("summary");
      clear(summary);
      for (const [label, value] of [
        ["Remote devices", data.relay.device_count],
        ["Pending events", data.relay.pending_event_count],
        ["Acked events", data.relay.acked_event_count],
        ["Uptime seconds", data.relay.uptime_seconds],
      ]) {
        const stat = el("div", "stat");
        stat.append(el("span", "muted", label));
        stat.append(el("strong", "", String(value)));
        summary.append(stat);
      }

      const devices = document.getElementById("devices");
      clear(devices);
      if (!data.devices.length) devices.append(el("div", "row muted", "No remote devices registered."));
      for (const device of data.devices) {
        const row = el("div", "row");
        row.append(el("strong", "", device.id));
        row.append(el("div", "muted", `${device.type || "unknown"} ${device.model || ""}`.trim()));
        row.append(el("div", "muted", `Last seen ${timeText(device.last_seen)}`));
        devices.append(row);
      }

      const events = document.getElementById("events");
      clear(events);
      if (!data.recent_events.length) events.append(el("div", "row muted", "No relay events recorded."));
      for (const event of data.recent_events.slice().reverse()) {
        const row = el("div", "row");
        row.append(el("strong", "", `${event.event_type} from ${event.device_id}`));
        row.append(el("div", "muted", `${timeText(event.received_at)} · attempts ${event.attempts} · ${event.acked_at ? "acked" : "pending"}`));
        events.append(row);
      }

      const home = document.getElementById("home");
      clear(home);
      if (!data.home) {
        home.append(el("div", "row muted", "No home server snapshot received."));
      } else {
        const payload = data.home.payload || {};
        const row = el("div", "row");
        row.append(el("strong", "", `Snapshot ${timeText(data.home.received_at)}`));
        const summary = payload.summary || {};
        row.append(el("div", "muted", `Devices ${summary.device_count ?? "?"}, online ${summary.online_count ?? "?"}, recent buttons ${summary.recent_button_event_count ?? "?"}`));
        home.append(row);
      }

      const homeDevices = document.getElementById("homeDevices");
      clear(homeDevices);
      const homePayload = data.home?.payload || {};
      const devicesFromHome = Array.isArray(homePayload.devices) ? homePayload.devices : [];
      if (!devicesFromHome.length) {
        homeDevices.append(el("div", "row muted", "No home devices in the latest snapshot."));
      }
      for (const device of devicesFromHome) {
        const row = el("details", "row");
        row.open = false;
        const summaryNode = el("summary", "");
        const name = device.display_name || device.friendly_name || device.id || "unknown device";
        summaryNode.append(el("strong", "", name));
        summaryNode.append(el("span", "muted", ` ${device.online ? "online" : "offline"}${device.online_detail ? `, ${device.online_detail}` : ""}`));
        row.append(summaryNode);
        const meta = el("div", "device-meta");
        for (const [label, value] of [
          ["ID", device.id],
          ["Type", device.type],
          ["Model", device.model],
          ["Firmware", device.firmware_version || device.firmware?.version],
          ["Seen via", device.last_seen_via],
          ["Last seen", timeText(device.last_seen)],
          ["Last local", timeText(device.last_local_seen)],
          ["Last relay", timeText(device.last_relay_seen)],
          ["Pending events", device.pending_events],
          ["Capabilities", Array.isArray(device.capabilities) ? device.capabilities.join(", ") : ""],
        ]) {
          if (value !== undefined && value !== null && value !== "") {
            meta.append(el("div", "muted", `${label}: ${value}`));
          }
        }
        row.append(meta);
        row.append(jsonBlock(device));
        homeDevices.append(row);
      }
    }
    async function load() {
      auth.hidden = true;
      const response = await fetch("/dashboard-data", {cache: "no-store", headers: authHeaders()});
      if (response.status === 401) {
        statusEl.textContent = "Dashboard token required.";
        auth.hidden = false;
        return;
      }
      if (!response.ok) {
        statusEl.textContent = `Failed to load dashboard: HTTP ${response.status}`;
        return;
      }
      render(await response.json());
    }
    load();
    setInterval(load, 5000);
  </script>
</body>
</html>
"""


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "SpokenCommandRelay/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/health":
            json_response(self, 200, {"ok": True, "service": "spoken-command-relay"})
            return

        if parsed.path in ("/", "/dashboard"):
            html_response(self, 200, DASHBOARD_HTML)
            return

        if parsed.path == "/dashboard-data":
            if DASHBOARD_TOKEN:
                auth = require_static_token(self, DASHBOARD_TOKEN, "dashboard")
                if not auth.ok:
                    auth_error(self, auth)
                    return
            with STATE_LOCK:
                payload = relay_snapshot()
            json_response(self, 200, payload)
            return

        if parsed.path == "/sync/events":
            auth = require_static_token(self, SYNC_TOKEN, "sync")
            if not auth.ok:
                auth_error(self, auth)
                return
            try:
                limit = max(1, min(int(query.get("limit", ["20"])[0]), 100))
            except ValueError:
                limit = 20
            with STATE_LOCK:
                events = pending_events(limit)
            json_response(self, 200, {"ok": True, "events": events})
            return

        if parsed.path in ("/admin/devices", "/admin/events"):
            auth = require_static_token(self, ADMIN_TOKEN, "admin")
            if not auth.ok:
                auth_error(self, auth)
                return
            with STATE_LOCK:
                snapshot = relay_snapshot()
            if parsed.path == "/admin/devices":
                json_response(self, 200, {"ok": True, "devices": snapshot["devices"]})
            else:
                json_response(self, 200, {"ok": True, "events": snapshot["recent_events"]})
            return

        json_response(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path.startswith("/devices/") and parsed.path.endswith("/register"):
            device_id = clean_id(parsed.path.removeprefix("/devices/").removesuffix("/register"))
            auth, device_secret = authorize_registration(self, device_id)
            if not auth.ok:
                auth_error(self, auth)
                return
            try:
                payload = read_json_body(self)
                with STATE_LOCK:
                    device = upsert_device(device_id, payload, self)
                    event = queue_register_event(device_id, payload)
                response = {"ok": True, "device": device, "relay_event": event}
                if device_secret:
                    response["device_secret"] = device_secret
                json_response(self, 200, response)
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path.startswith("/devices/") and parsed.path.endswith("/button"):
            device_id = clean_id(parsed.path.removeprefix("/devices/").removesuffix("/button"))
            auth = require_device_token(self, device_id)
            if not auth.ok:
                auth_error(self, auth)
                return
            try:
                payload = read_json_body(self)
                with STATE_LOCK:
                    device = upsert_device(device_id, {"status": {"last_button_at": int(time.time())}}, self)
                    event = queue_button_event(device_id, payload, self)
                json_response(self, 200, {"ok": True, "device": device, "relay_event": event})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/sync/dashboard-snapshot":
            auth = require_static_token(self, SYNC_TOKEN, "sync")
            if not auth.ok:
                auth_error(self, auth)
                return
            try:
                payload = read_json_body(self)
                with STATE_LOCK:
                    store_dashboard_snapshot(payload)
                json_response(self, 200, {"ok": True})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path.startswith("/sync/events/") and parsed.path.endswith("/ack"):
            auth = require_static_token(self, SYNC_TOKEN, "sync")
            if not auth.ok:
                auth_error(self, auth)
                return
            event_id = clean_id(parsed.path.removeprefix("/sync/events/").removesuffix("/ack"))
            try:
                payload = read_json_body(self)
                with STATE_LOCK:
                    found = ack_event(event_id, payload)
                if not found:
                    json_response(self, 404, {"ok": False, "error": "event not found"})
                    return
                json_response(self, 200, {"ok": True, "event_id": event_id})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        json_response(self, 404, {"ok": False, "error": "not found"})


def main() -> None:
    init_database()
    if not DEVICE_ENROLL_TOKEN:
        print("WARNING: RELAY_DEVICE_ENROLL_TOKEN is not configured; new device enrollment will reject requests.")
    if not SYNC_TOKEN:
        print("WARNING: RELAY_SYNC_TOKEN is not configured; sync endpoints will reject requests.")
    if not DASHBOARD_TOKEN:
        print("WARNING: RELAY_DASHBOARD_TOKEN is not configured; dashboard data is public.")
    print(f"Starting spoken-command relay on {HOST}:{PORT}")
    server = ThreadingHTTPServer((HOST, PORT), RelayHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
