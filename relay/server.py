#!/usr/bin/env python3
"""Public relay for remote spoken-command devices.

The relay intentionally exposes a much smaller surface than the local command
server. Remote devices can register and enqueue events, while the home server
polls those events using an outbound authenticated connection.
"""

from __future__ import annotations

import hmac
import hashlib
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
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


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

STATE_LOCK = threading.RLock()
DASHBOARD_CODE: dict[str, Any] = {}
DASHBOARD_SESSIONS: dict[str, int] = {}
DASHBOARD_CODE_LAST_REQUEST_AT = 0


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
                status_json TEXT NOT NULL DEFAULT '{}',
                status_dirty INTEGER NOT NULL DEFAULT 0
            )
        """)
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(devices)").fetchall()
        }
        if "status_dirty" not in columns:
            connection.execute("ALTER TABLE devices ADD COLUMN status_dirty INTEGER NOT NULL DEFAULT 0")
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
            CREATE INDEX IF NOT EXISTS idx_events_recent
            ON events(received_at DESC)
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_snapshots (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                received_at INTEGER NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS paired_devices (
                device_id TEXT PRIMARY KEY,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                remote_addr TEXT,
                user_agent TEXT,
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


def cleanup_dashboard_sessions(now: int | None = None) -> None:
    current = int(time.time()) if now is None else now
    expired = [token for token, expires_at in DASHBOARD_SESSIONS.items() if int(expires_at) <= current]
    for token in expired:
        DASHBOARD_SESSIONS.pop(token, None)


def dashboard_session_valid(provided: str) -> bool:
    now = int(time.time())
    cleanup_dashboard_sessions(now)
    expires_at = DASHBOARD_SESSIONS.get(provided)
    return bool(provided and expires_at and int(expires_at) > now)


def require_dashboard_access(handler: BaseHTTPRequestHandler) -> AuthResult:
    provided = token_from_header(handler)
    if DASHBOARD_TOKEN and token_matches(DASHBOARD_TOKEN, provided):
        return AuthResult(True, HTTPStatus.OK, "")
    with STATE_LOCK:
        if dashboard_session_valid(provided):
            return AuthResult(True, HTTPStatus.OK, "")
    if DASHBOARD_TOKEN or NTFY_TOPIC or IP_PAIRING_TOKEN:
        return AuthResult(False, HTTPStatus.UNAUTHORIZED, "unauthorized")
    return AuthResult(True, HTTPStatus.OK, "")


def send_ntfy_message(message: str, title: str) -> None:
    if not NTFY_TOPIC:
        raise RuntimeError("RELAY_NTFY_TOPIC is not configured")
    url = f"{NTFY_URL}/{NTFY_TOPIC}"
    headers = {
        "Title": title,
        "Priority": "high",
        "Tags": "key",
        "User-Agent": "SpokenCommandRelay/0.1",
    }
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    request = Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=8) as response:
            response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:240]
        raise RuntimeError(f"ntfy returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"ntfy request failed: {exc.reason}") from exc


def request_dashboard_code(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    global DASHBOARD_CODE_LAST_REQUEST_AT
    now = int(time.time())
    with STATE_LOCK:
        if not NTFY_TOPIC:
            raise RuntimeError("RELAY_NTFY_TOPIC is not configured")
        if DASHBOARD_CODE_LAST_REQUEST_AT and now - DASHBOARD_CODE_LAST_REQUEST_AT < DASHBOARD_CODE_REQUEST_SECONDS:
            wait_seconds = DASHBOARD_CODE_REQUEST_SECONDS - (now - DASHBOARD_CODE_LAST_REQUEST_AT)
            return {"sent": False, "retry_after_seconds": wait_seconds}
        code = f"{secrets.randbelow(100_000_000):08d}"
    send_ntfy_message(
        f"Relay dashboard code: {code}\n\nExpires in {DASHBOARD_CODE_TTL_SECONDS // 60} minutes.",
        NTFY_TITLE,
    )
    with STATE_LOCK:
        now = int(time.time())
        DASHBOARD_CODE.clear()
        DASHBOARD_CODE.update({
            "hash": hashlib.sha256(code.encode("utf-8")).hexdigest(),
            "expires_at": now + DASHBOARD_CODE_TTL_SECONDS,
            "attempts": 0,
            "requested_by": client_ip(handler),
        })
        DASHBOARD_CODE_LAST_REQUEST_AT = now
    return {"sent": True, "expires_in_seconds": DASHBOARD_CODE_TTL_SECONDS}


def verify_dashboard_code(code: str) -> dict[str, Any]:
    now = int(time.time())
    cleaned = re.sub(r"\D+", "", code)[:8]
    if len(cleaned) != 8:
        raise ValueError("code must be 8 digits")
    with STATE_LOCK:
        expected = str(DASHBOARD_CODE.get("hash", ""))
        expires_at = int(DASHBOARD_CODE.get("expires_at", 0) or 0)
        attempts = int(DASHBOARD_CODE.get("attempts", 0) or 0)
        if not expected or expires_at <= now:
            DASHBOARD_CODE.clear()
            raise ValueError("code expired")
        if attempts >= 5:
            DASHBOARD_CODE.clear()
            raise ValueError("too many attempts")
        DASHBOARD_CODE["attempts"] = attempts + 1
        if not token_matches(expected, hashlib.sha256(cleaned.encode("utf-8")).hexdigest()):
            raise ValueError("invalid code")
        DASHBOARD_CODE.clear()
        session_token = secrets.token_urlsafe(32)
        expires_at = now + DASHBOARD_SESSION_SECONDS
        cleanup_dashboard_sessions(now)
        DASHBOARD_SESSIONS[session_token] = expires_at
    return {"session_token": session_token, "expires_at": expires_at, "expires_in_seconds": DASHBOARD_SESSION_SECONDS}


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
            return AuthResult(True, HTTPStatus.OK, ""), existing
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


def paired_device_from_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "id": row["device_id"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "remote_addr": row["remote_addr"],
        "user_agent": row["user_agent"],
        "name": payload.get("name") or row["device_id"],
        "type": payload.get("type", "computer"),
        "hostname": payload.get("hostname", ""),
        "local_ips": payload.get("local_ips") if isinstance(payload.get("local_ips"), list) else [],
        "external_ip": payload.get("external_ip") or row["remote_addr"],
        "ports": payload.get("ports") if isinstance(payload.get("ports"), list) else [],
        "notes": payload.get("notes", ""),
        "payload": payload,
    }


def paired_devices() -> list[dict[str, Any]]:
    with db_connect() as connection:
        rows = connection.execute("SELECT * FROM paired_devices ORDER BY device_id ASC").fetchall()
    return [paired_device_from_row(row) for row in rows]


def clean_ip_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = re.split(r"[\s,]+", value)
    elif isinstance(value, list):
        items = [str(item) for item in value]
    else:
        items = []
    result: list[str] = []
    for item in items:
        cleaned = item.strip()[:80]
        if cleaned:
            result.append(cleaned)
    return result[:12]


def clean_port_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = re.split(r"[\s,]+", value)
    elif isinstance(value, list):
        items = [str(item) for item in value]
    else:
        items = []
    result: list[str] = []
    for item in items:
        cleaned = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", item.strip())[:40]
        if cleaned:
            result.append(cleaned)
    return result[:20]


def upsert_paired_device(device_id: str, payload: dict[str, Any], handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    now = int(time.time())
    public_ip = client_ip(handler)
    record = {
        "name": str(payload.get("name", device_id)).strip()[:120] or device_id,
        "type": str(payload.get("type", "computer")).strip()[:60] or "computer",
        "hostname": str(payload.get("hostname", "")).strip()[:120],
        "local_ips": clean_ip_list(payload.get("local_ips", payload.get("local_ip", []))),
        "external_ip": str(payload.get("external_ip", "")).strip()[:80] or public_ip,
        "ports": clean_port_list(payload.get("ports", [])),
        "notes": str(payload.get("notes", "")).strip()[:500],
    }
    with db_connect() as connection:
        connection.execute(
            """
            INSERT INTO paired_devices (device_id, first_seen, last_seen, remote_addr, user_agent, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                remote_addr = excluded.remote_addr,
                user_agent = excluded.user_agent,
                payload_json = excluded.payload_json
            """,
            (
                device_id,
                now,
                now,
                public_ip,
                handler.headers.get("User-Agent", ""),
                json.dumps(record, separators=(",", ":")),
            ),
        )
        row = connection.execute("SELECT * FROM paired_devices WHERE device_id = ?", (device_id,)).fetchone()
    return paired_device_from_row(row)


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


def upsert_device(
    device_id: str,
    payload: dict[str, Any],
    handler: BaseHTTPRequestHandler,
    mark_status_dirty: bool = False,
) -> dict[str, Any]:
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
                payload_json, status_json, status_dirty
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                remote_addr = excluded.remote_addr,
                user_agent = excluded.user_agent,
                payload_json = excluded.payload_json,
                status_json = excluded.status_json,
                status_dirty = CASE
                    WHEN excluded.status_dirty = 1 THEN 1
                    ELSE devices.status_dirty
                END
            """,
            (
                device_id,
                now,
                now,
                client_ip(handler),
                handler.headers.get("User-Agent", ""),
                json.dumps(merged_payload, separators=(",", ":")),
                json.dumps(merged_status, separators=(",", ":")),
                1 if mark_status_dirty else 0,
            ),
        )
        row = connection.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
    return public_device(row)


def dirty_device_statuses(limit: int = 100) -> list[dict[str, Any]]:
    with db_connect() as connection:
        rows = connection.execute(
            "SELECT * FROM devices WHERE status_dirty = 1 ORDER BY last_seen ASC LIMIT ?",
            (limit,),
        ).fetchall()
        device_ids = [row["device_id"] for row in rows]
        if device_ids:
            placeholders = ",".join("?" for _ in device_ids)
            connection.execute(
                f"UPDATE devices SET status_dirty = 0 WHERE device_id IN ({placeholders})",
                device_ids,
            )

    devices: list[dict[str, Any]] = []
    for row in rows:
        device = public_device(row)
        devices.append({
            "id": device["id"],
            "last_seen": device.get("last_seen"),
            "remote_addr": device.get("remote_addr", ""),
            "user_agent": device.get("user_agent", ""),
            "status": device.get("status", {}),
        })
    return devices


def prune_acked_events(connection: sqlite3.Connection) -> int:
    if MAX_EVENT_ROWS <= 0:
        return 0
    total = connection.execute("SELECT count(*) AS count FROM events").fetchone()["count"]
    excess = int(total) - MAX_EVENT_ROWS
    if excess <= 0:
        return 0
    cursor = connection.execute(
        """
        DELETE FROM events
        WHERE id IN (
            SELECT id FROM events
            WHERE acked_at IS NOT NULL
            ORDER BY received_at ASC
            LIMIT ?
        )
        """,
        (excess,),
    )
    return cursor.rowcount


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
        prune_acked_events(connection)
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


def clean_mission_task_type(value: str) -> str:
    cleaned = str(value).strip().lower()
    if cleaned in {"daily", "today"}:
        return "daily"
    return "persistent"


def queue_mission_task_create(payload: dict[str, Any], handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    title = re.sub(r"\s+", " ", str(payload.get("title", "")).strip())[:160]
    if not title:
        raise ValueError("title is required")
    task_type = clean_mission_task_type(str(payload.get("task_type", "persistent")))
    due_date = str(payload.get("due_date", "")).strip()[:10]
    event = {
        "title": title,
        "notes": str(payload.get("notes", ""))[:1000],
        "task_type": task_type,
        "due_date": due_date,
        "created_by": "relay-dashboard",
        "remote_addr": client_ip(handler),
    }
    return enqueue_event("relay-dashboard", "mission_task_create", event)


def queue_mission_task_complete(payload: dict[str, Any], handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    task_id = clean_id(str(payload.get("id", "")))
    if not task_id:
        raise ValueError("task id is required")
    event = {
        "id": task_id,
        "completed_by": "relay-dashboard",
        "remote_addr": client_ip(handler),
    }
    return enqueue_event("relay-dashboard", "mission_task_complete", event)


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
        found = cursor.rowcount > 0
        if found:
            prune_acked_events(connection)
        return found


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
        paired_rows = connection.execute("SELECT * FROM paired_devices ORDER BY device_id ASC").fetchall()
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
    paired = [paired_device_from_row(row) for row in paired_rows]
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
            "has_dashboard_code_auth": bool(NTFY_TOPIC),
            "has_ip_pairing_token": bool(IP_PAIRING_TOKEN),
        },
        "devices": devices,
        "paired_devices": paired,
        "recent_events": recent_events,
        "home": home,
    }


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dracon Relay</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      color-scheme: dark;
      font-family: "IBM Plex Sans", system-ui, "Segoe UI", sans-serif;
      --bg: #0f0c08;
      --panel: #181310;
      --panel-soft: #110d0a;
      --raised: #211a14;
      --text: #ece4d8;
      --bright: #faf5ec;
      --muted: #9b8d7a;
      --border: #2e2519;
      --border-bright: #473a25;
      --accent: #f0a839;
      --accent-soft: rgba(240, 168, 57, 0.13);
      --accent-glow: rgba(240, 168, 57, 0.35);
      --warn: #ffd166;
      --warn-soft: rgba(255, 209, 102, 0.12);
      --ok: #6bd58c;
      --ok-soft: rgba(107, 213, 140, 0.12);
      --bad: #ff7a64;
      --bad-soft: rgba(255, 122, 100, 0.12);
      --display: "Chakra Petch", "Segoe UI", sans-serif;
      --mono: "IBM Plex Mono", ui-monospace, Consolas, monospace;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--text);
      background:
        radial-gradient(1100px 520px at 80% -10%, rgba(240, 168, 57, 0.07), transparent 60%),
        radial-gradient(800px 460px at -10% 110%, rgba(240, 168, 57, 0.04), transparent 55%),
        repeating-linear-gradient(0deg, rgba(240, 168, 57, 0.02) 0 1px, transparent 1px 28px),
        repeating-linear-gradient(90deg, rgba(240, 168, 57, 0.02) 0 1px, transparent 1px 28px),
        var(--bg);
      background-attachment: fixed;
      font-size: 14px;
      line-height: 1.45;
    }
    ::selection { background: var(--accent); color: #2b1c04; }
    @keyframes beacon-rise {
      from { opacity: 0; transform: translateY(10px); }
      to { opacity: 1; transform: translateY(0); }
    }
    @keyframes beacon-pulse {
      0%, 100% { opacity: 1; box-shadow: 0 0 12px var(--accent-glow); }
      50% { opacity: 0.55; box-shadow: 0 0 4px var(--accent-glow); }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { animation: none !important; transition: none !important; }
    }
    header {
      border-bottom: 1px solid var(--border);
      background: linear-gradient(180deg, rgba(24, 19, 16, 0.97), rgba(24, 19, 16, 0.9));
      backdrop-filter: blur(6px);
      box-shadow: 0 1px 0 rgba(240, 168, 57, 0.16), 0 12px 30px rgba(8, 5, 2, 0.5);
      animation: beacon-rise 0.4s ease-out backwards;
    }
    .header-inner {
      max-width: 1180px;
      margin: 0 auto;
      padding: 16px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    main {
      max-width: 1180px;
      margin: 0 auto;
      padding: 22px 24px 36px;
      animation: beacon-rise 0.5s ease-out 0.1s backwards;
    }
    h1 {
      margin: 0;
      font-family: var(--display);
      font-size: 1.25rem;
      font-weight: 700;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--bright);
    }
    h1::before {
      content: "";
      display: inline-block;
      width: 10px;
      height: 10px;
      margin-right: 12px;
      border-radius: 50%;
      background: var(--accent);
      animation: beacon-pulse 2.8s ease-in-out infinite;
      vertical-align: 1px;
    }
    h2 {
      margin: 0;
      font-family: var(--display);
      font-size: 0.85rem;
      font-weight: 600;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--accent);
    }
    h2::before { content: "// "; color: var(--border-bright); }
    .muted {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 0.78rem;
    }
    .top-status {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      text-align: right;
      white-space: nowrap;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 10px;
      margin-top: 16px;
    }
    .tabs {
      display: flex;
      flex-wrap: wrap;
      gap: 2px;
      margin: 20px 0 0;
      border-bottom: 1px solid var(--border);
    }
    .tab-button {
      border: 0;
      border-bottom: 2px solid transparent;
      border-radius: 0;
      background: transparent;
      height: 36px;
      padding: 0 14px;
      font-family: var(--display);
      font-size: 0.78rem;
      font-weight: 600;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .tab-button:hover { color: var(--text); border-color: var(--border-bright); box-shadow: none; }
    .tab-button.active {
      color: var(--accent);
      border-bottom-color: var(--accent);
      background: linear-gradient(180deg, transparent 55%, var(--accent-soft));
      text-shadow: 0 0 14px var(--accent-glow);
    }
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }
    .stat, .row {
      border: 1px solid var(--border);
      border-radius: 4px;
      background: linear-gradient(180deg, var(--raised) 0%, var(--panel) 70%);
    }
    .stat {
      padding: 13px 14px;
      border-top: 1px solid var(--border-bright);
      clip-path: polygon(0 0, calc(100% - 14px) 0, 100% 14px, 100% 100%, 0 100%);
      position: relative;
      animation: beacon-rise 0.45s ease-out backwards;
    }
    .stat:nth-child(2) { animation-delay: 0.05s; }
    .stat:nth-child(3) { animation-delay: 0.1s; }
    .stat:nth-child(4) { animation-delay: 0.15s; }
    .stat:nth-child(5) { animation-delay: 0.2s; }
    .stat:nth-child(6) { animation-delay: 0.25s; }
    .stat::before {
      content: "";
      position: absolute;
      left: 0;
      top: 0;
      bottom: 0;
      width: 3px;
      background: linear-gradient(180deg, var(--accent), transparent 85%);
      opacity: 0.85;
    }
    .stat strong {
      display: block;
      font-family: var(--display);
      font-size: 1.6rem;
      font-weight: 700;
      margin-top: 4px;
      color: var(--bright);
      text-shadow: 0 0 18px var(--accent-soft);
    }
    .stat span {
      font-family: var(--mono);
      text-transform: uppercase;
      letter-spacing: 0.12em;
      font-size: 0.66rem;
      color: var(--muted);
    }
    .rows { display: grid; gap: 8px; }
    .section-title {
      margin: 24px 0 10px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .section-title .muted { font-size: 0.74rem; }
    details.panel { margin-top: 24px; }
    details.panel > summary { cursor: pointer; list-style-position: outside; color: var(--accent); }
    details.panel > summary .section-title {
      display: inline-flex;
      width: calc(100% - 22px);
      margin: 0 0 10px 4px;
      vertical-align: middle;
    }
    .row { padding: 12px 14px; transition: border-color 0.15s; }
    .row:hover { border-color: var(--border-bright); }
    .row-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
    }
    .row-title { min-width: 0; }
    .row-title strong { overflow-wrap: anywhere; color: var(--bright); }
    .meta-line { margin-top: 3px; }
    .device-meta {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(185px, 1fr));
      gap: 6px 14px;
      margin-top: 10px;
    }
    .kv { min-width: 0; }
    .kv span {
      display: block;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 0.66rem;
      text-transform: uppercase;
      letter-spacing: 0.12em;
    }
    .kv strong {
      display: block;
      font-family: var(--mono);
      font-size: 0.82rem;
      overflow-wrap: anywhere;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      border-radius: 2px;
      border: 1px solid transparent;
      padding: 3px 8px;
      font-family: var(--mono);
      font-size: 0.72rem;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      white-space: nowrap;
    }
    .pill.ok { color: var(--ok); background: var(--ok-soft); border-color: rgba(107, 213, 140, 0.3); }
    .pill.warn { color: var(--warn); background: var(--warn-soft); border-color: rgba(255, 209, 102, 0.3); }
    .pill.bad { color: var(--bad); background: var(--bad-soft); border-color: rgba(255, 122, 100, 0.3); }
    .pill.neutral { color: var(--accent); background: var(--accent-soft); border-color: rgba(240, 168, 57, 0.3); }
    .chips { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 10px; }
    .chip {
      border: 1px solid var(--border-bright);
      border-radius: 2px;
      padding: 2px 7px;
      color: var(--muted);
      background: var(--panel-soft);
      font-family: var(--mono);
      font-size: 0.7rem;
      letter-spacing: 0.03em;
    }
    details.raw { margin-top: 10px; }
    details.raw > summary { cursor: pointer; color: var(--muted); font-family: var(--mono); font-size: 0.74rem; }
    pre {
      margin: 8px 0 0;
      max-height: 360px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: var(--mono);
      font-size: 0.74rem;
      background: var(--panel-soft);
      border: 1px solid var(--border);
      border-radius: 3px;
      padding: 10px;
      color: var(--accent);
    }
    code { font-family: var(--mono); }
    button, input { font: inherit; }
    input {
      min-width: 0;
      padding: 8px 10px;
      border: 1px solid var(--border-bright);
      border-radius: 3px;
      background: var(--raised);
      color: var(--text);
    }
    select, textarea {
      min-width: 0;
      font: inherit;
      padding: 8px 10px;
      border: 1px solid var(--border-bright);
      border-radius: 3px;
      background: var(--raised);
      color: var(--text);
    }
    input::placeholder, textarea::placeholder { color: var(--muted); opacity: 0.7; }
    input:focus-visible, select:focus-visible, textarea:focus-visible, button:focus-visible {
      outline: 1px solid var(--accent);
      outline-offset: 1px;
    }
    #token { min-width: 260px; }
    textarea { min-height: 72px; resize: vertical; }
    button {
      padding: 8px 12px;
      border: 1px solid var(--border-bright);
      border-radius: 3px;
      background: var(--raised);
      color: var(--text);
      cursor: pointer;
      font-family: var(--display);
      font-weight: 600;
      letter-spacing: 0.06em;
      transition: border-color 0.15s, box-shadow 0.15s, color 0.15s;
    }
    button:hover {
      border-color: var(--accent);
      color: var(--accent);
      box-shadow: 0 0 10px var(--accent-soft);
    }
    .action-form { display: grid; gap: 8px; }
    .form-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 150px) minmax(0, 160px);
      gap: 8px;
    }
    #missionForm input, #missionForm select, #missionForm textarea { width: 100%; max-width: 100%; }
    .pairing-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
    #pairingForm input, #pairingForm select, #pairingForm textarea { width: 100%; max-width: 100%; }
    .form-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .auth-panel {
      margin-top: 16px;
      padding: 14px;
      border: 1px solid var(--border);
      border-top: 2px solid var(--accent);
      border-radius: 4px;
      background: linear-gradient(180deg, var(--raised) 0%, var(--panel) 70%);
    }
    .auth-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
    .auth-row input { min-width: 180px; }
    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--border-bright); border-radius: 5px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--accent); }
    @media (max-width: 680px) {
      .header-inner { align-items: flex-start; flex-direction: column; }
      .top-status { justify-content: flex-start; text-align: left; }
      main { padding-inline: 14px; }
      .form-grid, .pairing-grid { grid-template-columns: 1fr; }
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
      <div class="muted top-status">
        <span id="status">Loading...</span>
        <button id="logoutButton" type="button" hidden>Logout</button>
      </div>
    </div>
  </header>
  <main>
    <section id="auth" class="auth-panel" hidden>
      <h2>Dashboard Access</h2>
      <div class="muted" id="authMessage">Request a temporary code on your phone.</div>
      <div class="auth-row">
        <button id="requestCodeButton" type="button">Send Phone Code</button>
        <input id="code" type="text" inputmode="numeric" maxlength="8" autocomplete="one-time-code" placeholder="8-digit code">
        <button id="verifyCodeButton" type="button">Verify</button>
      </div>
      <form id="tokenAuth" class="auth-row">
        <input id="token" type="password" autocomplete="current-password" placeholder="Dashboard token">
        <button type="submit">Save Token</button>
      </form>
    </section>
    <section class="grid" id="summary"></section>
    <nav class="tabs" aria-label="Relay dashboard sections">
      <button class="tab-button active" type="button" data-tab="events">Events</button>
      <button class="tab-button" type="button" data-tab="mission">Mission</button>
      <button class="tab-button" type="button" data-tab="pairing">Pairing</button>
      <button class="tab-button" type="button" data-tab="devices">Devices</button>
      <button class="tab-button" type="button" data-tab="uptime">Uptime</button>
    </nav>
    <section class="tab-panel active" data-tab-panel="events">
      <div class="section-title"><h2>Recent Relay Events</h2><span class="muted" id="relayEventCount"></span></div>
      <section class="rows" id="events"></section>
    </section>
    <section class="tab-panel" data-tab-panel="mission">
      <div class="section-title"><h2>Mission Board</h2><span class="muted" id="missionCount"></span></div>
      <div class="row">
        <form class="action-form" id="missionForm">
          <div class="form-grid">
            <input id="missionTitle" type="text" placeholder="Task title" autocomplete="off">
            <select id="missionType">
              <option value="persistent">Persistent</option>
              <option value="daily">Today only</option>
            </select>
            <input id="missionDueDate" type="date">
          </div>
          <textarea id="missionNotes" placeholder="Notes, optional"></textarea>
          <div class="form-actions">
            <button type="submit">Add Task</button>
            <span class="muted" id="missionFormResult"></span>
          </div>
        </form>
      </div>
      <section class="rows" id="missionTasks"></section>
    </section>
    <section class="tab-panel" data-tab-panel="pairing">
      <div class="section-title">
        <h2>IP Pairing</h2>
        <div class="form-actions">
          <span class="muted" id="pairedDeviceCount"></span>
          <button id="openPairingForm" type="button">Register</button>
        </div>
      </div>
      <div class="row" id="pairingFormPanel" hidden>
        <form class="action-form" id="pairingForm">
          <div class="pairing-grid">
            <input id="pairingDeviceId" type="text" placeholder="Device ID" autocomplete="off">
            <input id="pairingName" type="text" placeholder="Name" autocomplete="off">
            <input id="pairingType" type="text" placeholder="Type, e.g. windows-pc" autocomplete="off">
            <input id="pairingHostname" type="text" placeholder="Hostname" autocomplete="off">
            <input id="pairingLocalIps" type="text" placeholder="Local IPs, comma separated" autocomplete="off">
            <input id="pairingExternalIp" type="text" placeholder="External IP" autocomplete="off">
            <input id="pairingPorts" type="text" placeholder="Ports, e.g. ssh:22,rdp:3389" autocomplete="off">
          </div>
          <textarea id="pairingNotes" placeholder="Notes, optional"></textarea>
          <div class="form-actions">
            <button type="submit">Save Pairing</button>
            <button id="closePairingForm" type="button">Cancel</button>
            <span class="muted" id="pairingFormResult"></span>
          </div>
        </form>
      </div>
      <section class="rows" id="pairedDevices"></section>
    </section>
    <section class="tab-panel" data-tab-panel="devices">
      <div class="section-title"><h2>Remote Devices</h2><span class="muted" id="remoteDeviceCount"></span></div>
      <section class="rows" id="devices"></section>
      <div class="section-title"><h2>Home Devices</h2><span class="muted" id="homeDeviceCount"></span></div>
      <section class="rows" id="homeDevices"></section>
    </section>
    <section class="tab-panel" data-tab-panel="uptime">
      <div class="section-title"><h2>Home Snapshot</h2><span class="muted" id="homeSnapshotAge"></span></div>
      <section class="rows" id="home"></section>
      <div class="section-title"><h2>Uptime Checks</h2><span class="muted" id="uptimeCount"></span></div>
      <section class="rows" id="uptimeMonitors"></section>
    </section>
  </main>
  <script>
    const tokenKey = "draconRelayDashboardToken";
    const tabKey = "draconRelayDashboardTab";
    const statusEl = document.getElementById("status");
    const auth = document.getElementById("auth");
    const authMessage = document.getElementById("authMessage");
    const token = document.getElementById("token");
    const code = document.getElementById("code");
    const logoutButton = document.getElementById("logoutButton");
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
    function setActiveTab(tabName) {
      document.querySelectorAll(".tab-button").forEach((button) => {
        button.classList.toggle("active", button.dataset.tab === tabName);
      });
      document.querySelectorAll(".tab-panel").forEach((panel) => {
        panel.classList.toggle("active", panel.dataset.tabPanel === tabName);
      });
      localStorage.setItem(tabKey, tabName);
    }
    function initTabs() {
      document.querySelectorAll(".tab-button").forEach((button) => {
        button.addEventListener("click", () => setActiveTab(button.dataset.tab));
      });
      const saved = localStorage.getItem(tabKey) || "events";
      setActiveTab(document.querySelector(`[data-tab="${saved}"]`) ? saved : "events");
    }
    function authHeaders() {
      const saved = localStorage.getItem(tokenKey) || "";
      return saved ? {Authorization: `Bearer ${saved}`} : {};
    }
    function todayText() {
      const now = new Date();
      const month = String(now.getMonth() + 1).padStart(2, "0");
      const day = String(now.getDate()).padStart(2, "0");
      return `${now.getFullYear()}-${month}-${day}`;
    }
    function setAuthenticated(authenticated) {
      auth.hidden = authenticated;
      logoutButton.hidden = !authenticated;
    }
    function clearDashboard() {
      for (const id of ["summary", "devices", "events", "home", "homeDevices", "uptimeMonitors", "missionTasks", "pairedDevices"]) {
        clear(document.getElementById(id));
      }
      for (const id of ["remoteDeviceCount", "relayEventCount", "homeSnapshotAge", "homeDeviceCount", "uptimeCount", "missionCount", "missionFormResult", "pairedDeviceCount", "pairingFormResult"]) {
        document.getElementById(id).textContent = "";
      }
    }
    document.getElementById("tokenAuth").addEventListener("submit", (event) => {
      event.preventDefault();
      localStorage.setItem(tokenKey, token.value);
      token.value = "";
      load();
    });
    document.getElementById("requestCodeButton").addEventListener("click", async () => {
      authMessage.textContent = "Sending code...";
      const response = await fetch("/dashboard-auth/request", {method: "POST", cache: "no-store"});
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.ok === false) {
        authMessage.textContent = payload.error || `Failed to send code: HTTP ${response.status}`;
        return;
      }
      if (payload.sent === false) {
        authMessage.textContent = `Code was requested recently. Try again in ${payload.retry_after_seconds || 60}s.`;
        return;
      }
      authMessage.textContent = `Code sent. It expires in ${payload.expires_in_seconds || 300}s.`;
      code.focus();
    });
    document.getElementById("verifyCodeButton").addEventListener("click", async () => {
      authMessage.textContent = "Verifying code...";
      const response = await fetch("/dashboard-auth/verify", {
        method: "POST",
        cache: "no-store",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({code: code.value}),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.ok === false) {
        authMessage.textContent = payload.error || `Failed to verify code: HTTP ${response.status}`;
        return;
      }
      localStorage.setItem(tokenKey, payload.session_token);
      code.value = "";
      authMessage.textContent = "Access granted.";
      load();
    });
    logoutButton.addEventListener("click", () => {
      localStorage.removeItem(tokenKey);
      clearDashboard();
      statusEl.textContent = "Logged out.";
      authMessage.textContent = "Request a temporary code on your phone.";
      setAuthenticated(false);
    });
    async function createMissionTask(event) {
      event.preventDefault();
      const result = document.getElementById("missionFormResult");
      const taskType = document.getElementById("missionType").value;
      const payload = {
        title: document.getElementById("missionTitle").value,
        notes: document.getElementById("missionNotes").value,
        task_type: taskType,
        due_date: taskType === "daily" ? (document.getElementById("missionDueDate").value || todayText()) : "",
      };
      result.textContent = "Queuing...";
      const response = await fetch("/mission-board/tasks", {
        method: "POST",
        cache: "no-store",
        headers: {"Content-Type": "application/json", ...authHeaders()},
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) {
        result.textContent = data.error || `Failed: HTTP ${response.status}`;
        return;
      }
      document.getElementById("missionTitle").value = "";
      document.getElementById("missionNotes").value = "";
      result.textContent = "Queued for home sync.";
      load();
    }
    async function completeMissionTask(taskId, button) {
      button.disabled = true;
      button.textContent = "Queuing...";
      const response = await fetch(`/mission-board/tasks/${encodeURIComponent(taskId)}/complete`, {
        method: "POST",
        cache: "no-store",
        headers: {"Content-Type": "application/json", ...authHeaders()},
        body: JSON.stringify({completed_by: "relay-dashboard"}),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) {
        button.disabled = false;
        button.textContent = data.error || "Failed";
        return;
      }
      button.textContent = "Queued";
      load();
    }
    function csvList(value) {
      return String(value || "").split(",").map((item) => item.trim()).filter(Boolean);
    }
    async function openPairingFormPanel() {
      const panel = document.getElementById("pairingFormPanel");
      const result = document.getElementById("pairingFormResult");
      panel.hidden = false;
      result.textContent = "Reading external IP...";
      try {
        const response = await fetch("/dashboard-client-info", {cache: "no-store", headers: authHeaders()});
        const data = await response.json().catch(() => ({}));
        if (response.ok && data.external_ip) {
          document.getElementById("pairingExternalIp").value = data.external_ip;
          result.textContent = "External IP filled.";
        } else {
          result.textContent = "External IP unavailable.";
        }
      } catch (error) {
        result.textContent = "External IP unavailable.";
      }
      document.getElementById("pairingDeviceId").focus();
    }
    function closePairingFormPanel() {
      document.getElementById("pairingFormPanel").hidden = true;
      document.getElementById("pairingFormResult").textContent = "";
    }
    async function registerPairedDevice(event) {
      event.preventDefault();
      const result = document.getElementById("pairingFormResult");
      const payload = {
        device_id: document.getElementById("pairingDeviceId").value,
        name: document.getElementById("pairingName").value,
        type: document.getElementById("pairingType").value,
        hostname: document.getElementById("pairingHostname").value,
        local_ips: csvList(document.getElementById("pairingLocalIps").value),
        external_ip: document.getElementById("pairingExternalIp").value,
        ports: csvList(document.getElementById("pairingPorts").value),
        notes: document.getElementById("pairingNotes").value,
      };
      if (!payload.device_id.trim()) {
        result.textContent = "Device ID is required.";
        return;
      }
      result.textContent = "Saving...";
      const response = await fetch("/dashboard/paired-devices", {
        method: "POST",
        cache: "no-store",
        headers: {"Content-Type": "application/json", ...authHeaders()},
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.ok === false) {
        result.textContent = data.error || `Failed: HTTP ${response.status}`;
        return;
      }
      result.textContent = "Saved.";
      document.getElementById("pairingForm").reset();
      closePairingFormPanel();
      load();
    }
    function renderMissionBoard(homePayload) {
      const root = document.getElementById("missionTasks");
      clear(root);
      const board = homePayload.mission_board || {};
      const tasks = Array.isArray(board.tasks) ? board.tasks : [];
      document.getElementById("missionCount").textContent = `${tasks.length} active`;
      document.getElementById("missionDueDate").value = board.today || todayText();
      if (!tasks.length) {
        root.append(el("div", "row muted", "No active mission tasks."));
        return;
      }
      for (const task of tasks) {
        const row = el("div", "row");
        const head = el("div", "row-head");
        const title = el("div", "row-title");
        title.append(el("strong", "", task.title || "Untitled task"));
        const detail = task.task_type === "daily" ? `today only · ${task.due_date || board.today || ""}` : "persistent";
        title.append(el("div", "muted meta-line", detail));
        head.append(title);
        const done = el("button", "", "Complete");
        done.type = "button";
        done.addEventListener("click", () => completeMissionTask(task.id, done));
        head.append(done);
        row.append(head);
        if (task.notes) row.append(el("div", "muted meta-line", task.notes));
        const meta = el("div", "device-meta");
        meta.append(kv("ID", task.id));
        meta.append(kv("Created", timeText(task.created_at)));
        meta.append(kv("Source", task.source));
        row.append(meta);
        root.append(row);
      }
    }
    function renderPairedDevices(devices) {
      const root = document.getElementById("pairedDevices");
      clear(root);
      const paired = Array.isArray(devices) ? devices : [];
      document.getElementById("pairedDeviceCount").textContent = `${paired.length} paired`;
      if (!paired.length) {
        root.append(el("div", "row muted", "No paired IP devices."));
        return;
      }
      for (const device of paired) {
        const row = el("div", "row");
        const head = el("div", "row-head");
        const title = el("div", "row-title");
        title.append(el("strong", "", device.name || device.id));
        title.append(el("div", "muted meta-line", `${device.type || "computer"}${device.hostname ? ` · ${device.hostname}` : ""}`));
        head.append(title);
        head.append(pill("paired", "neutral"));
        row.append(head);
        const meta = el("div", "device-meta");
        meta.append(kv("External IP", device.external_ip || device.remote_addr));
        meta.append(kv("Local IPs", Array.isArray(device.local_ips) ? device.local_ips.join(", ") : ""));
        meta.append(kv("Ports", Array.isArray(device.ports) ? device.ports.join(", ") : ""));
        meta.append(kv("Last update", timeText(device.last_seen)));
        meta.append(kv("First seen", timeText(device.first_seen)));
        meta.append(kv("Remote address", device.remote_addr));
        row.append(meta);
        if (device.notes) row.append(el("div", "muted meta-line", device.notes));
        row.append(jsonBlock(device.payload || {}));
        root.append(row);
      }
    }
    function render(data) {
      setAuthenticated(true);
      statusEl.textContent = `Updated ${new Date().toLocaleTimeString()}`;
      const summary = document.getElementById("summary");
      clear(summary);
      for (const [label, value] of [
        ["Remote devices", data.relay.device_count],
        ["Paired IP devices", (data.paired_devices || []).length],
        ["Pending events", data.relay.pending_event_count],
        ["Acked events", data.relay.acked_event_count],
        ["Relay uptime", formatUptime(data.relay.uptime_seconds)],
      ]) {
        const stat = el("div", "stat");
        stat.append(el("span", "muted", label));
        stat.append(el("strong", "", String(value)));
        summary.append(stat);
      }

      renderPairedDevices(data.paired_devices);

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
      renderMissionBoard(homePayload);
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

      const uptimeMonitors = document.getElementById("uptimeMonitors");
      clear(uptimeMonitors);
      const monitors = Array.isArray(homePayload.uptime_monitors) ? homePayload.uptime_monitors : [];
      const onlineChecks = monitors.filter((monitor) => monitor.online).length;
      document.getElementById("uptimeCount").textContent = `${onlineChecks}/${monitors.length} online`;
      if (!monitors.length) {
        uptimeMonitors.append(el("div", "row muted", "No uptime checks in the latest home snapshot."));
      }
      for (const monitor of monitors) {
        const row = el("div", "row");
        const head = el("div", "row-head");
        const title = el("div", "row-title");
        title.append(el("strong", "", monitor.name || monitor.id || "uptime check"));
        title.append(el("div", "muted meta-line", monitor.target || ""));
        head.append(title);
        head.append(statusPill(monitor.online, monitor.detail));
        row.append(head);
        const meta = el("div", "device-meta");
        meta.append(kv("ID", monitor.id));
        meta.append(kv("Last checked", timeText(monitor.last_checked_at)));
        meta.append(kv("Next check", timeText(monitor.next_check_at)));
        meta.append(kv("Interval", `${monitor.interval_seconds || 0}s`));
        meta.append(kv("Latency", monitor.latency_ms === null || monitor.latency_ms === undefined ? "" : `${monitor.latency_ms}ms`));
        meta.append(kv("Status code", monitor.status_code));
        meta.append(kv("Enabled", monitor.enabled === false ? "no" : "yes"));
        row.append(meta);
        row.append(jsonBlock(monitor));
        uptimeMonitors.append(row);
      }
    }
    async function load() {
      const response = await fetch("/dashboard-data", {cache: "no-store", headers: authHeaders()});
      if (response.status === 401) {
        statusEl.textContent = "Dashboard token required.";
        clearDashboard();
        setAuthenticated(false);
        return;
      }
      if (!response.ok) {
        statusEl.textContent = `Failed to load dashboard: HTTP ${response.status}`;
        return;
      }
      render(await response.json());
    }
    document.getElementById("missionForm").addEventListener("submit", createMissionTask);
    document.getElementById("openPairingForm").addEventListener("click", openPairingFormPanel);
    document.getElementById("closePairingForm").addEventListener("click", closePairingFormPanel);
    document.getElementById("pairingForm").addEventListener("submit", registerPairedDevice);
    document.getElementById("missionDueDate").value = todayText();
    initTabs();
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
            auth = require_dashboard_access(self)
            if not auth.ok:
                auth_error(self, auth)
                return
            with STATE_LOCK:
                payload = relay_snapshot()
            json_response(self, 200, payload)
            return

        if parsed.path == "/dashboard-client-info":
            auth = require_dashboard_access(self)
            if not auth.ok:
                auth_error(self, auth)
                return
            json_response(self, 200, {"ok": True, "external_ip": client_ip(self)})
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

        if parsed.path == "/sync/device-statuses":
            auth = require_static_token(self, SYNC_TOKEN, "sync")
            if not auth.ok:
                auth_error(self, auth)
                return
            try:
                limit = max(1, min(int(query.get("limit", ["100"])[0]), 500))
            except ValueError:
                limit = 100
            with STATE_LOCK:
                devices = dirty_device_statuses(limit)
            json_response(self, 200, {"ok": True, "devices": devices, "server_time": int(time.time())})
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

        if parsed.path == "/dashboard-auth/request":
            try:
                payload = request_dashboard_code(self)
                json_response(self, 200, {"ok": True, **payload})
            except Exception as exc:
                json_response(self, 503, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/dashboard-auth/verify":
            try:
                payload = read_json_body(self)
                session = verify_dashboard_code(str(payload.get("code", "")))
                json_response(self, 200, {"ok": True, **session})
            except ValueError as exc:
                json_response(self, 401, {"ok": False, "error": str(exc)})
            except Exception as exc:
                json_response(self, 500, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/mission-board/tasks":
            auth = require_dashboard_access(self)
            if not auth.ok:
                auth_error(self, auth)
                return
            try:
                payload = read_json_body(self)
                with STATE_LOCK:
                    event = queue_mission_task_create(payload, self)
                json_response(self, 202, {"ok": True, "relay_event": event})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path.startswith("/mission-board/tasks/") and parsed.path.endswith("/complete"):
            auth = require_dashboard_access(self)
            if not auth.ok:
                auth_error(self, auth)
                return
            task_id = clean_id(parsed.path.removeprefix("/mission-board/tasks/").removesuffix("/complete"))
            try:
                payload = read_json_body(self)
                payload["id"] = task_id
                with STATE_LOCK:
                    event = queue_mission_task_complete(payload, self)
                json_response(self, 202, {"ok": True, "relay_event": event})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/dashboard/paired-devices":
            auth = require_dashboard_access(self)
            if not auth.ok:
                auth_error(self, auth)
                return
            try:
                payload = read_json_body(self)
                device_id = clean_id(str(payload.get("device_id", "")))
                if not device_id:
                    raise ValueError("device_id is required")
                with STATE_LOCK:
                    device = upsert_paired_device(device_id, payload, self)
                json_response(self, 200, {"ok": True, "device": device})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path.startswith("/paired-devices/"):
            auth = require_static_token(self, IP_PAIRING_TOKEN, "ip pairing")
            if not auth.ok:
                auth_error(self, auth)
                return
            device_id = clean_id(parsed.path.removeprefix("/paired-devices/"))
            try:
                payload = read_json_body(self)
                with STATE_LOCK:
                    device = upsert_paired_device(device_id, payload, self)
                json_response(self, 200, {"ok": True, "device": device})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

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
                response = {"ok": True}
                if device_secret:
                    response["device_secret"] = device_secret
                response["device"] = device
                response["relay_event"] = event
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

        if parsed.path.startswith("/devices/") and parsed.path.endswith("/status"):
            device_id = clean_id(parsed.path.removeprefix("/devices/").removesuffix("/status"))
            auth = require_device_token(self, device_id)
            if not auth.ok:
                auth_error(self, auth)
                return
            try:
                payload = read_json_body(self)
                with STATE_LOCK:
                    device = upsert_device(device_id, payload, self, mark_status_dirty=True)
                json_response(self, 200, {"ok": True, "device": device})
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
