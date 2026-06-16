"""HTTP request/response helpers for the relay handler."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any

from . import config

if TYPE_CHECKING:
    from .auth import AuthResult


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


def auth_error(handler: BaseHTTPRequestHandler, result: "AuthResult") -> None:
    json_response(handler, result.status, {"ok": False, "error": result.message})


def read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length_text = handler.headers.get("Content-Length", "")
    if not length_text:
        return {}
    try:
        length = int(length_text)
    except ValueError as exc:
        raise ValueError("invalid content length") from exc
    if length > config.MAX_JSON_BYTES:
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
