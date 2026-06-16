"""HTTP request handler and route dispatch for the relay."""

from __future__ import annotations

import time
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import config, store
from .auth import (
    authorize_registration,
    request_dashboard_code,
    require_dashboard_access,
    require_device_token,
    require_static_token,
    verify_dashboard_code,
)
from .dashboard import dashboard_html
from .http_util import auth_error, client_ip, html_response, json_response, read_json_body
from .util import clean_id


def _query_limit(query: dict[str, list[str]], key: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(query.get(key, [str(default)])[0]), high))
    except ValueError:
        return default


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "SpokenCommandRelay/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    # --- GET ----------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path

        if path == "/health":
            json_response(self, 200, {"ok": True, "service": "spoken-command-relay"})
            return

        if path in ("/", "/dashboard"):
            html_response(self, 200, dashboard_html())
            return

        if path == "/dashboard-data":
            if not self._require(require_dashboard_access(self)):
                return
            with config.STATE_LOCK:
                payload = store.relay_snapshot()
            json_response(self, 200, payload)
            return

        if path == "/dashboard-client-info":
            if not self._require(require_dashboard_access(self)):
                return
            json_response(self, 200, {"ok": True, "external_ip": client_ip(self)})
            return

        if path == "/sync/events":
            if not self._require(require_static_token(self, config.SYNC_TOKEN, "sync")):
                return
            limit = _query_limit(query, "limit", 20, 1, 100)
            with config.STATE_LOCK:
                events = store.pending_events(limit)
            json_response(self, 200, {"ok": True, "events": events})
            return

        if path == "/sync/device-statuses":
            if not self._require(require_static_token(self, config.SYNC_TOKEN, "sync")):
                return
            limit = _query_limit(query, "limit", 100, 1, 500)
            with config.STATE_LOCK:
                devices = store.dirty_device_statuses(limit)
            json_response(self, 200, {"ok": True, "devices": devices, "server_time": int(time.time())})
            return

        if path in ("/admin/devices", "/admin/events"):
            if not self._require(require_static_token(self, config.ADMIN_TOKEN, "admin")):
                return
            with config.STATE_LOCK:
                if path == "/admin/devices":
                    json_response(self, 200, {"ok": True, "devices": store.device_list()})
                else:
                    events = list(reversed(store.recent_events(config.RECENT_LIMIT)))
                    json_response(self, 200, {"ok": True, "events": events})
            return

        json_response(self, 404, {"ok": False, "error": "not found"})

    # --- POST ---------------------------------------------------------------

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/dashboard-auth/request":
            try:
                payload = request_dashboard_code(self)
                json_response(self, 200, {"ok": True, **payload})
            except Exception as exc:
                json_response(self, 503, {"ok": False, "error": str(exc)})
            return

        if path == "/dashboard-auth/verify":
            try:
                payload = read_json_body(self)
                session = verify_dashboard_code(str(payload.get("code", "")))
                json_response(self, 200, {"ok": True, **session})
            except ValueError as exc:
                json_response(self, 401, {"ok": False, "error": str(exc)})
            except Exception as exc:
                json_response(self, 500, {"ok": False, "error": str(exc)})
            return

        if path == "/mission-board/tasks":
            if not self._require(require_dashboard_access(self)):
                return
            try:
                payload = read_json_body(self)
                with config.STATE_LOCK:
                    event = store.queue_mission_task_create(payload, self)
                json_response(self, 202, {"ok": True, "relay_event": event})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if path.startswith("/mission-board/tasks/") and path.endswith("/complete"):
            if not self._require(require_dashboard_access(self)):
                return
            task_id = clean_id(path.removeprefix("/mission-board/tasks/").removesuffix("/complete"))
            try:
                payload = read_json_body(self)
                payload["id"] = task_id
                with config.STATE_LOCK:
                    event = store.queue_mission_task_complete(payload, self)
                json_response(self, 202, {"ok": True, "relay_event": event})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if path == "/dashboard/paired-devices":
            if not self._require(require_dashboard_access(self)):
                return
            try:
                payload = read_json_body(self)
                device_id = clean_id(str(payload.get("device_id", "")))
                if not device_id:
                    raise ValueError("device_id is required")
                with config.STATE_LOCK:
                    device = store.upsert_paired_device(device_id, payload, self)
                json_response(self, 200, {"ok": True, "device": device})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if path.startswith("/paired-devices/"):
            if not self._require(require_static_token(self, config.IP_PAIRING_TOKEN, "ip pairing")):
                return
            device_id = clean_id(path.removeprefix("/paired-devices/"))
            try:
                payload = read_json_body(self)
                with config.STATE_LOCK:
                    device = store.upsert_paired_device(device_id, payload, self)
                json_response(self, 200, {"ok": True, "device": device})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if path.startswith("/devices/") and path.endswith("/register"):
            device_id = clean_id(path.removeprefix("/devices/").removesuffix("/register"))
            auth, device_secret = authorize_registration(self, device_id)
            if not self._require(auth):
                return
            try:
                payload = read_json_body(self)
                with config.STATE_LOCK:
                    device = store.upsert_device(device_id, payload, self)
                    event = store.queue_register_event(device_id, payload)
                response = {"ok": True}
                if device_secret:
                    response["device_secret"] = device_secret
                response["device"] = device
                response["relay_event"] = event
                json_response(self, 200, response)
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if path.startswith("/devices/") and path.endswith("/button"):
            device_id = clean_id(path.removeprefix("/devices/").removesuffix("/button"))
            if not self._require(require_device_token(self, device_id)):
                return
            try:
                payload = read_json_body(self)
                with config.STATE_LOCK:
                    device = store.upsert_device(device_id, {"status": {"last_button_at": int(time.time())}}, self)
                    event = store.queue_button_event(device_id, payload, self)
                json_response(self, 200, {"ok": True, "device": device, "relay_event": event})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if path.startswith("/devices/") and path.endswith("/status"):
            device_id = clean_id(path.removeprefix("/devices/").removesuffix("/status"))
            if not self._require(require_device_token(self, device_id)):
                return
            try:
                payload = read_json_body(self)
                with config.STATE_LOCK:
                    device = store.upsert_device(device_id, payload, self, mark_status_dirty=True)
                json_response(self, 200, {"ok": True, "device": device})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if path == "/sync/dashboard-snapshot":
            if not self._require(require_static_token(self, config.SYNC_TOKEN, "sync")):
                return
            try:
                payload = read_json_body(self)
                with config.STATE_LOCK:
                    store.store_dashboard_snapshot(payload)
                json_response(self, 200, {"ok": True})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if path.startswith("/sync/events/") and path.endswith("/ack"):
            if not self._require(require_static_token(self, config.SYNC_TOKEN, "sync")):
                return
            event_id = clean_id(path.removeprefix("/sync/events/").removesuffix("/ack"))
            try:
                payload = read_json_body(self)
                with config.STATE_LOCK:
                    found = store.ack_event(event_id, payload)
                if not found:
                    json_response(self, 404, {"ok": False, "error": "event not found"})
                    return
                json_response(self, 200, {"ok": True, "event_id": event_id})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        json_response(self, 404, {"ok": False, "error": "not found"})

    # --- helpers ------------------------------------------------------------

    def _require(self, result) -> bool:
        """Return True when authorised; otherwise emit the error response."""
        if result.ok:
            return True
        auth_error(self, result)
        return False
