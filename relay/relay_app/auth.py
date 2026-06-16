"""Authentication: static tokens, device tokens, dashboard codes/sessions."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import config
from .http_util import client_ip
from .util import clean_id


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    status: int
    message: str


# In-memory dashboard auth state, guarded by config.STATE_LOCK.
DASHBOARD_CODE: dict[str, Any] = {}
DASHBOARD_SESSIONS: dict[str, int] = {}
DASHBOARD_CODE_LAST_REQUEST_AT = 0


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


# --- Dashboard codes and sessions -------------------------------------------

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
    if config.DASHBOARD_TOKEN and token_matches(config.DASHBOARD_TOKEN, provided):
        return AuthResult(True, HTTPStatus.OK, "")
    with config.STATE_LOCK:
        if dashboard_session_valid(provided):
            return AuthResult(True, HTTPStatus.OK, "")
    if config.DASHBOARD_TOKEN or config.NTFY_TOPIC or config.IP_PAIRING_TOKEN:
        return AuthResult(False, HTTPStatus.UNAUTHORIZED, "unauthorized")
    return AuthResult(True, HTTPStatus.OK, "")


def send_ntfy_message(message: str, title: str) -> None:
    if not config.NTFY_TOPIC:
        raise RuntimeError("RELAY_NTFY_TOPIC is not configured")
    url = f"{config.NTFY_URL}/{config.NTFY_TOPIC}"
    headers = {
        "Title": title,
        "Priority": "high",
        "Tags": "key",
        "User-Agent": "SpokenCommandRelay/0.1",
    }
    if config.NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {config.NTFY_TOKEN}"
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
    with config.STATE_LOCK:
        if not config.NTFY_TOPIC:
            raise RuntimeError("RELAY_NTFY_TOPIC is not configured")
        if DASHBOARD_CODE_LAST_REQUEST_AT and now - DASHBOARD_CODE_LAST_REQUEST_AT < config.DASHBOARD_CODE_REQUEST_SECONDS:
            wait_seconds = config.DASHBOARD_CODE_REQUEST_SECONDS - (now - DASHBOARD_CODE_LAST_REQUEST_AT)
            return {"sent": False, "retry_after_seconds": wait_seconds}
        code = f"{secrets.randbelow(100_000_000):08d}"
    send_ntfy_message(
        f"Relay dashboard code: {code}\n\nExpires in {config.DASHBOARD_CODE_TTL_SECONDS // 60} minutes.",
        config.NTFY_TITLE,
    )
    with config.STATE_LOCK:
        now = int(time.time())
        DASHBOARD_CODE.clear()
        DASHBOARD_CODE.update({
            "hash": hashlib.sha256(code.encode("utf-8")).hexdigest(),
            "expires_at": now + config.DASHBOARD_CODE_TTL_SECONDS,
            "attempts": 0,
            "requested_by": client_ip(handler),
        })
        DASHBOARD_CODE_LAST_REQUEST_AT = now
    return {"sent": True, "expires_in_seconds": config.DASHBOARD_CODE_TTL_SECONDS}


def verify_dashboard_code(code: str) -> dict[str, Any]:
    now = int(time.time())
    cleaned = re.sub(r"\D+", "", code)[:8]
    if len(cleaned) != 8:
        raise ValueError("code must be 8 digits")
    with config.STATE_LOCK:
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
        expires_at = now + config.DASHBOARD_SESSION_SECONDS
        cleanup_dashboard_sessions(now)
        DASHBOARD_SESSIONS[session_token] = expires_at
    return {
        "session_token": session_token,
        "expires_at": expires_at,
        "expires_in_seconds": config.DASHBOARD_SESSION_SECONDS,
    }


# --- Device tokens (cached file) --------------------------------------------
#
# The token file is read on every device request. We cache the parsed mapping
# and only re-read when the file's mtime/size changes, guarded by a dedicated
# lock so it does not contend with STATE_LOCK.

_TOKENS_LOCK = threading.Lock()
_TOKENS_CACHE: dict[str, str] = {}
_TOKENS_STAMP: tuple[float, int] | None = None


def _parse_device_tokens(payload: Any) -> dict[str, str]:
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


def load_device_tokens() -> dict[str, str]:
    global _TOKENS_CACHE, _TOKENS_STAMP
    with _TOKENS_LOCK:
        try:
            stat = os.stat(config.DEVICE_TOKENS_PATH)
            stamp = (stat.st_mtime, stat.st_size)
        except FileNotFoundError:
            _TOKENS_CACHE = {}
            _TOKENS_STAMP = None
            return {}
        if stamp == _TOKENS_STAMP:
            return dict(_TOKENS_CACHE)
        try:
            with open(config.DEVICE_TOKENS_PATH, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return dict(_TOKENS_CACHE)
        _TOKENS_CACHE = _parse_device_tokens(payload)
        _TOKENS_STAMP = stamp
        return dict(_TOKENS_CACHE)


def save_device_tokens(tokens: dict[str, str]) -> None:
    global _TOKENS_CACHE, _TOKENS_STAMP
    directory = os.path.dirname(config.DEVICE_TOKENS_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = {"devices": {device_id: tokens[device_id] for device_id in sorted(tokens)}}
    temp_path = f"{config.DEVICE_TOKENS_PATH}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    os.replace(temp_path, config.DEVICE_TOKENS_PATH)
    with _TOKENS_LOCK:
        _TOKENS_CACHE = dict(tokens)
        try:
            stat = os.stat(config.DEVICE_TOKENS_PATH)
            _TOKENS_STAMP = (stat.st_mtime, stat.st_size)
        except OSError:
            _TOKENS_STAMP = None


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
        if token_matches(config.DEVICE_ENROLL_TOKEN, provided):
            return AuthResult(True, HTTPStatus.OK, ""), existing
        return AuthResult(False, HTTPStatus.UNAUTHORIZED, "unauthorized"), None

    if not config.DEVICE_ENROLL_TOKEN:
        return AuthResult(False, HTTPStatus.SERVICE_UNAVAILABLE, "device enrollment token is not configured"), None
    if not token_matches(config.DEVICE_ENROLL_TOKEN, provided):
        return AuthResult(False, HTTPStatus.UNAUTHORIZED, "unauthorized"), None

    device_secret = generate_device_token()
    tokens[device_id] = device_secret
    save_device_tokens(tokens)
    return AuthResult(True, HTTPStatus.OK, ""), device_secret
