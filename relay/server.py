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
    :root {
      color-scheme: light dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif;
      --bg: #f4f6f8;
      --panel: #ffffff;
      --panel-soft: #f9faf8;
      --text: #20252b;
      --muted: #667085;
      --border: #d9dee3;
      --accent: #0f766e;
      --accent-soft: #e6f4f1;
      --warn: #b7791f;
      --warn-soft: #fff4db;
      --ok: #27745a;
      --ok-soft: #e8f5ee;
      --bad: #9a3412;
      --bad-soft: #fff0e8;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); }
    header { border-bottom: 1px solid var(--border); background: var(--panel); }
    .header-inner { max-width: 1180px; margin: 0 auto; padding: 18px 24px; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
    main { max-width: 1180px; margin: 0 auto; padding: 22px 24px 36px; }
    h1 { margin: 0; font-size: 1.35rem; }
    h2 { margin: 0; font-size: 1rem; }
    .muted { color: var(--muted); font-size: .88rem; }
    .top-status { text-align: right; white-space: nowrap; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin-top: 16px; }
    .stat, .row { border: 1px solid var(--border); border-radius: 8px; background: var(--panel); }
    .stat { padding: 13px 14px; border-top: 3px solid var(--accent); }
    .stat strong { display: block; font-size: 1.55rem; margin-top: 4px; }
    .stat span { text-transform: uppercase; letter-spacing: .04em; font-size: .72rem; }
    .rows { display: grid; gap: 8px; }
    .section-title { margin: 24px 0 10px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .section-title .muted { font-size: .82rem; }
    details.panel { margin-top: 24px; }
    details.panel > summary { cursor: pointer; list-style-position: outside; }
    details.panel > summary .section-title { display: inline-flex; width: calc(100% - 22px); margin: 0 0 10px 4px; vertical-align: middle; }
    .row { padding: 12px 14px; }
    .row-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
    .row-title { min-width: 0; }
    .row-title strong { overflow-wrap: anywhere; }
    .meta-line { margin-top: 3px; }
    .device-meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(185px, 1fr)); gap: 6px 14px; margin-top: 10px; }
    .kv { min-width: 0; }
    .kv span { display: block; color: var(--muted); font-size: .72rem; text-transform: uppercase; letter-spacing: .04em; }
    .kv strong { display: block; font-size: .9rem; overflow-wrap: anywhere; }
    .pill { display: inline-flex; align-items: center; border-radius: 999px; padding: 3px 8px; font-size: .78rem; font-weight: 650; white-space: nowrap; }
    .pill.ok { color: var(--ok); background: var(--ok-soft); }
    .pill.warn { color: var(--warn); background: var(--warn-soft); }
    .pill.bad { color: var(--bad); background: var(--bad-soft); }
    .pill.neutral { color: var(--accent); background: var(--accent-soft); }
    .chips { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 10px; }
    .chip { border: 1px solid var(--border); border-radius: 999px; padding: 2px 7px; color: var(--muted); background: var(--panel-soft); font-size: .76rem; }
    details.raw { margin-top: 10px; }
    details.raw > summary { cursor: pointer; color: var(--muted); font-size: .82rem; }
    pre { margin: 8px 0 0; max-height: 360px; overflow: auto; white-space: pre-wrap; word-break: break-word; font-size: .8rem; background: var(--panel-soft); border: 1px solid var(--border); border-radius: 6px; padding: 10px; }
    code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
    button, input { font: inherit; }
    input { min-width: 260px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 6px; }
    button { padding: 8px 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel); color: var(--text); }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #171a1f;
        --panel: #22262d;
        --panel-soft: #191d23;
        --text: #e7e9ed;
        --muted: #aab2c0;
        --border: #363d47;
        --accent: #5eead4;
        --accent-soft: #123b37;
        --warn: #f4c766;
        --warn-soft: #3c2d12;
        --ok: #7dd3aa;
        --ok-soft: #173528;
        --bad: #ffb088;
        --bad-soft: #3f2117;
      }
    }
    @media (max-width: 680px) {
      .header-inner { align-items: flex-start; flex-direction: column; }
      .top-status { text-align: left; }
      main { padding-inline: 14px; }
      .row-head, .section-title { align-items: flex-start; flex-direction: column; }
    }
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <div>
        <h1>Dracon Relay</h1>
        <div class="muted">Public relay and home sync status</div>
      </div>
      <div id="status" class="muted top-status">Loading...</div>
    </div>
  </header>
  <main>
    <form id="auth" hidden>
      <input id="token" type="password" autocomplete="current-password" placeholder="Dashboard token">
      <button type="submit">Save</button>
    </form>
    <section class="grid" id="summary"></section>
    <div class="section-title"><h2>Remote Devices</h2><span class="muted" id="remoteDeviceCount"></span></div>
    <section class="rows" id="devices"></section>
    <details class="panel" id="eventsPanel" open>
      <summary><div class="section-title"><h2>Recent Relay Events</h2><span class="muted" id="relayEventCount"></span></div></summary>
      <section class="rows" id="events"></section>
    </details>
    <div class="section-title"><h2>Home Snapshot</h2><span class="muted" id="homeSnapshotAge"></span></div>
    <section class="rows" id="home"></section>
    <div class="section-title"><h2>Home Devices</h2><span class="muted" id="homeDeviceCount"></span></div>
    <section class="rows" id="homeDevices"></section>
  </main>
  <script>
    const tokenKey = "draconRelayDashboardToken";
    const statusEl = document.getElementById("status");
    const auth = document.getElementById("auth");
    const token = document.getElementById("token");
    const openHomeDevices = new Set();

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
      const details = el("details", "raw");
      details.append(el("summary", "", "Full readout"));
      const block = el("pre", "");
      block.textContent = JSON.stringify(value, null, 2);
      details.append(block);
      return details;
    }
    function pill(text, kind) {
      return el("span", `pill ${kind || "neutral"}`, text);
    }
    function kv(label, value) {
      const node = el("div", "kv");
      node.append(el("span", "", label));
      node.append(el("strong", "", value === undefined || value === null || value === "" ? "-" : String(value)));
      return node;
    }
    function statusPill(online, detail) {
      if (online) return pill(detail || "online", "ok");
      if (detail) return pill(detail, "warn");
      return pill("offline", "bad");
    }
    function capabilityChips(capabilities) {
      const wrap = el("div", "chips");
      if (!Array.isArray(capabilities) || !capabilities.length) return wrap;
      for (const capability of capabilities) wrap.append(el("span", "chip", capability));
      return wrap;
    }
    function formatUptime(seconds) {
      const value = Number(seconds || 0);
      const days = Math.floor(value / 86400);
      const hours = Math.floor((value % 86400) / 3600);
      const minutes = Math.floor((value % 3600) / 60);
      if (days) return `${days}d ${hours}h`;
      if (hours) return `${hours}h ${minutes}m`;
      return `${minutes}m`;
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
        ["Relay uptime", formatUptime(data.relay.uptime_seconds)],
      ]) {
        const stat = el("div", "stat");
        stat.append(el("span", "muted", label));
        stat.append(el("strong", "", String(value)));
        summary.append(stat);
      }

      const devices = document.getElementById("devices");
      clear(devices);
      document.getElementById("remoteDeviceCount").textContent = `${data.devices.length} registered`;
      if (!data.devices.length) devices.append(el("div", "row muted", "No remote devices registered."));
      for (const device of data.devices) {
        const row = el("div", "row");
        const head = el("div", "row-head");
        const title = el("div", "row-title");
        title.append(el("strong", "", device.id));
        title.append(el("div", "muted meta-line", `${device.type || "unknown"} ${device.model || ""}`.trim()));
        head.append(title);
        head.append(pill("remote", "neutral"));
        row.append(head);
        const meta = el("div", "device-meta");
        meta.append(kv("Last seen", timeText(device.last_seen)));
        meta.append(kv("First seen", timeText(device.first_seen)));
        meta.append(kv("Remote address", device.remote_addr));
        row.append(meta);
        devices.append(row);
      }

      const events = document.getElementById("events");
      clear(events);
      document.getElementById("relayEventCount").textContent = `${data.recent_events.length} shown`;
      if (!data.recent_events.length) events.append(el("div", "row muted", "No relay events recorded."));
      for (const event of data.recent_events.slice().reverse()) {
        const row = el("div", "row");
        const head = el("div", "row-head");
        const title = el("div", "row-title");
        title.append(el("strong", "", `${event.event_type} from ${event.device_id}`));
        title.append(el("div", "muted meta-line", timeText(event.received_at)));
        head.append(title);
        head.append(pill(event.acked_at ? "acked" : "pending", event.acked_at ? "ok" : "warn"));
        row.append(head);
        const meta = el("div", "device-meta");
        meta.append(kv("Attempts", event.attempts));
        meta.append(kv("Delivered", timeText(event.delivered_at)));
        meta.append(kv("Acked", timeText(event.acked_at)));
        row.append(meta);
        row.append(jsonBlock(event.payload || {}));
        events.append(row);
      }

      const home = document.getElementById("home");
      clear(home);
      if (!data.home) {
        document.getElementById("homeSnapshotAge").textContent = "";
        home.append(el("div", "row muted", "No home server snapshot received."));
      } else {
        const payload = data.home.payload || {};
        const row = el("div", "row");
        document.getElementById("homeSnapshotAge").textContent = `received ${timeText(data.home.received_at)}`;
        const head = el("div", "row-head");
        const title = el("div", "row-title");
        title.append(el("strong", "", "Latest home sync"));
        title.append(el("div", "muted meta-line", `Snapshot ${timeText(data.home.received_at)}`));
        head.append(title);
        head.append(pill("synced", "ok"));
        row.append(head);
        const summary = payload.summary || {};
        const meta = el("div", "device-meta");
        meta.append(kv("Devices", summary.device_count ?? "?"));
        meta.append(kv("Online", summary.online_count ?? "?"));
        meta.append(kv("Offline", summary.offline_count ?? "?"));
        meta.append(kv("Recent buttons", summary.recent_button_event_count ?? "?"));
        meta.append(kv("Rules", summary.rule_count ?? "?"));
        meta.append(kv("Active timers", summary.active_timer_count ?? "?"));
        row.append(meta);
        home.append(row);
      }

      const homeDevices = document.getElementById("homeDevices");
      clear(homeDevices);
      const homePayload = data.home?.payload || {};
      const devicesFromHome = Array.isArray(homePayload.devices) ? homePayload.devices : [];
      document.getElementById("homeDeviceCount").textContent = `${devicesFromHome.length} in snapshot`;
      if (!devicesFromHome.length) {
        homeDevices.append(el("div", "row muted", "No home devices in the latest snapshot."));
      }
      for (const device of devicesFromHome) {
        const row = el("details", "row");
        const deviceKey = String(device.id || device.display_name || "");
        row.open = openHomeDevices.has(deviceKey);
        row.addEventListener("toggle", () => {
          if (!deviceKey) return;
          if (row.open) openHomeDevices.add(deviceKey);
          else openHomeDevices.delete(deviceKey);
        });
        const summaryNode = el("summary", "");
        summaryNode.className = "row-head";
        const title = el("div", "row-title");
        const name = device.display_name || device.friendly_name || device.id || "unknown device";
        title.append(el("strong", "", name));
        title.append(el("div", "muted meta-line", `${device.type || "unknown"}${device.model ? ` · ${device.model}` : ""}`));
        summaryNode.append(title);
        summaryNode.append(statusPill(device.online, device.online_detail));
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
            meta.append(kv(label, value));
          }
        }
        row.append(meta);
        row.append(capabilityChips(device.capabilities));
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
