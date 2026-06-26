"""Persistence layer: devices, events, paired devices, and snapshots."""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from http.server import BaseHTTPRequestHandler
from typing import Any

from . import config
from .db import db_connect
from .http_util import client_ip
from .util import clean_id, clean_ip_list, clean_port_list, clean_mission_task_type, json_obj


_SPOTIFY_CODES: dict[str, tuple[str, float]] = {}
_SPOTIFY_CODE_TTL = 600


def clean_spotify_state(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.:-]+", "", str(value).strip())[:128]


def clean_spotify_code(value: str) -> str:
    return str(value).strip()[:4096]


def store_spotify_code(state: str, code: str) -> None:
    cleaned_state = clean_spotify_state(state)
    cleaned_code = clean_spotify_code(code)
    if not cleaned_state or not cleaned_code:
        raise ValueError("state and code are required")
    _SPOTIFY_CODES[cleaned_state] = (cleaned_code, time.time())


def pop_spotify_code(state: str) -> str | None:
    cleaned_state = clean_spotify_state(state)
    if not cleaned_state:
        return None
    entry = _SPOTIFY_CODES.get(cleaned_state)
    if not entry:
        return None
    code, received_at = entry
    if time.time() - received_at > _SPOTIFY_CODE_TTL:
        _SPOTIFY_CODES.pop(cleaned_state, None)
        return None
    _SPOTIFY_CODES.pop(cleaned_state, None)
    return code


# --- Row -> dict projections ------------------------------------------------

def public_device(row: sqlite3.Row) -> dict[str, Any]:
    payload = json_obj(row["payload_json"])
    status = json_obj(row["status_json"])
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
    payload = json_obj(row["payload_json"])
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


def row_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "received_at": row["received_at"],
        "device_id": row["device_id"],
        "event_type": row["event_type"],
        "payload": json_obj(row["payload_json"]),
        "delivered_at": row["delivered_at"],
        "acked_at": row["acked_at"],
        "attempts": row["attempts"],
        "ack_ok": None if row["ack_ok"] is None else bool(row["ack_ok"]),
        "ack_error": row["ack_error"] or "",
    }


# --- Paired devices ---------------------------------------------------------

def paired_devices() -> list[dict[str, Any]]:
    with db_connect() as connection:
        rows = connection.execute("SELECT * FROM paired_devices ORDER BY device_id ASC").fetchall()
    return [paired_device_from_row(row) for row in rows]


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


# --- Devices ----------------------------------------------------------------

def upsert_device(
    device_id: str,
    payload: dict[str, Any],
    handler: BaseHTTPRequestHandler,
    mark_status_dirty: bool = False,
) -> dict[str, Any]:
    now = int(time.time())
    with db_connect() as connection:
        existing = connection.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        existing_payload = json_obj(existing["payload_json"]) if existing else {}
        existing_status = json_obj(existing["status_json"]) if existing else {}

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


def device_list() -> list[dict[str, Any]]:
    with db_connect() as connection:
        rows = connection.execute("SELECT * FROM devices ORDER BY device_id ASC").fetchall()
    return [public_device(row) for row in rows]


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


# --- Events -----------------------------------------------------------------

def prune_acked_events(connection: sqlite3.Connection) -> int:
    if config.MAX_EVENT_ROWS <= 0:
        return 0
    total = connection.execute("SELECT count(*) AS count FROM events").fetchone()["count"]
    excess = int(total) - config.MAX_EVENT_ROWS
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
    lease_cutoff = now - config.EVENT_LEASE_SECONDS
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


def recent_events(limit: int) -> list[dict[str, Any]]:
    """Most-recent-first list of events, capped at ``limit``."""
    with db_connect() as connection:
        rows = connection.execute(
            "SELECT * FROM events ORDER BY received_at DESC LIMIT ?",
            (limit,),
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


# --- Dashboard snapshot -----------------------------------------------------

def clean_note_text(value: str) -> str:
    return str(value).replace("\r\n", "\n").replace("\r", "\n")[:10000]


def public_r1_note(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    note = payload if isinstance(payload, dict) else {}
    return {
        "label": "r1-note",
        "text": clean_note_text(str(note.get("text", ""))),
        "updated_at": int(note.get("updated_at", 0) or 0),
    }


def store_r1_note(payload: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time())
    note = public_r1_note(payload)
    if not note["updated_at"]:
        note["updated_at"] = now
    with db_connect() as connection:
        connection.execute(
            """
            INSERT INTO r1_notes (id, received_at, note_json)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                received_at = excluded.received_at,
                note_json = excluded.note_json
            """,
            (now, json.dumps(note, separators=(",", ":"))),
        )
    return note


def latest_r1_note() -> dict[str, Any]:
    with db_connect() as connection:
        row = connection.execute("SELECT * FROM r1_notes WHERE id = 1").fetchone()
    if not row:
        return public_r1_note()
    note = public_r1_note(json_obj(row["note_json"]))
    note["received_at"] = row["received_at"]
    return note


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
    return {"received_at": row["received_at"], "payload": json_obj(row["payload_json"])}


def relay_snapshot() -> dict[str, Any]:
    with db_connect() as connection:
        device_rows = connection.execute("SELECT * FROM devices ORDER BY device_id ASC").fetchall()
        paired_rows = connection.execute("SELECT * FROM paired_devices ORDER BY device_id ASC").fetchall()
        recent_rows = connection.execute(
            "SELECT * FROM events ORDER BY received_at DESC LIMIT ?",
            (config.RECENT_LIMIT,),
        ).fetchall()
        pending_count = connection.execute(
            "SELECT count(*) AS count FROM events WHERE acked_at IS NULL",
        ).fetchone()["count"]
        acked_count = connection.execute(
            "SELECT count(*) AS count FROM events WHERE acked_at IS NOT NULL",
        ).fetchone()["count"]
    devices = [public_device(row) for row in device_rows]
    paired = [paired_device_from_row(row) for row in paired_rows]
    recent = [row_event(row) for row in reversed(recent_rows)]
    home = latest_dashboard_snapshot()
    note = latest_r1_note()
    return {
        "relay": {
            "host": config.HOST,
            "port": config.PORT,
            "started_at": config.SERVER_STARTED_AT,
            "uptime_seconds": int(time.time() - config.SERVER_STARTED_AT),
            "device_count": len(devices),
            "pending_event_count": pending_count,
            "acked_event_count": acked_count,
            "has_sync_token": bool(config.SYNC_TOKEN),
            "has_dashboard_token": bool(config.DASHBOARD_TOKEN),
            "has_dashboard_code_auth": bool(config.NTFY_TOPIC),
            "has_ip_pairing_token": bool(config.IP_PAIRING_TOKEN),
        },
        "devices": devices,
        "paired_devices": paired,
        "recent_events": recent,
        "home": home,
        "r1_note": note,
    }
