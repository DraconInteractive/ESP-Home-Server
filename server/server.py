#!/usr/bin/env python3
"""Local bridge server for ESP32 spoken-command audio.

The ESP32 posts either WAV bytes or raw signed 16-bit little-endian PCM. The
server wraps PCM as WAV, forwards it to ElevenLabs Speech to Text, interprets
the transcript as a local command, and returns a compact device response.
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlparse


HOST = os.environ.get("COMMAND_SERVER_HOST", "0.0.0.0")
PORT = int(os.environ.get("COMMAND_SERVER_PORT", "8080"))
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/speech-to-text"
MODEL_ID = os.environ.get("ELEVENLABS_MODEL_ID", "scribe_v2")
MAX_AUDIO_BYTES = int(os.environ.get("COMMAND_SERVER_MAX_AUDIO_BYTES", str(4 * 1024 * 1024)))
DEVICE_STALE_SECONDS = int(os.environ.get("COMMAND_SERVER_DEVICE_STALE_SECONDS", "45"))
DEVICE_PING_TIMEOUT_SECONDS = float(os.environ.get("COMMAND_SERVER_DEVICE_PING_TIMEOUT_SECONDS", "2.0"))
SERVER_STARTED_AT = int(time.time())

RECENT_COMMANDS: list[dict[str, Any]] = []
RECENT_BUTTON_EVENTS: list[dict[str, Any]] = []
MUTED_DEVICES: dict[str, bool] = {}
GLOBAL_MUTED = False
PENDING_ACTIONS: dict[str, dict[str, Any]] = {}
DEVICES: dict[str, dict[str, Any]] = {}
DEVICE_EVENTS: dict[str, list[dict[str, Any]]] = {}
STATE_LOCK = threading.RLock()


@dataclass(frozen=True)
class Command:
    name: str
    aliases: tuple[str, ...]
    description: str
    handler: Callable[[str, str, str], dict[str, Any]]


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def html_response(handler: BaseHTTPRequestHandler, status: int, body: str) -> None:
    encoded = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def read_request_body(handler: BaseHTTPRequestHandler) -> bytes:
    if handler.headers.get("Transfer-Encoding", "").lower() == "chunked":
        chunks: list[bytes] = []
        total = 0

        while True:
            size_line = handler.rfile.readline(64)
            if not size_line:
                raise ValueError("incomplete chunked request")
            size_text = size_line.split(b";", 1)[0].strip()
            chunk_size = int(size_text, 16)
            if chunk_size == 0:
                handler.rfile.readline(2)
                break
            total += chunk_size
            if total > MAX_AUDIO_BYTES:
                raise ValueError(f"audio body too large: {total} bytes")
            chunks.append(handler.rfile.read(chunk_size))
            if handler.rfile.read(2) != b"\r\n":
                raise ValueError("invalid chunk terminator")

        return b"".join(chunks)

    content_length = int(handler.headers.get("Content-Length", "0"))
    if content_length <= 0:
        raise ValueError("missing request body")
    if content_length > MAX_AUDIO_BYTES:
        raise ValueError(f"audio body too large: {content_length} bytes")
    return handler.rfile.read(content_length)


def pcm_s16le_to_wav(pcm: bytes, sample_rate: int, channels: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()


def multipart_form(fields: dict[str, str], file_field: str, filename: str, content_type: str, data: bytes) -> tuple[bytes, str]:
    boundary = f"----spoken-command-{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("ascii"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"))
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}\r\n".encode("ascii"))
    chunks.append(
        (
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("ascii")
    )
    chunks.append(data)
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))

    return b"".join(chunks), boundary


def transcribe_with_elevenlabs(wav_bytes: bytes) -> dict[str, Any]:
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")

    body, boundary = multipart_form(
        fields={"model_id": MODEL_ID},
        file_field="file",
        filename="command.wav",
        content_type="audio/wav",
        data=wav_bytes,
    )
    request = Request(
        ELEVENLABS_URL,
        data=body,
        headers={
            "xi-api-key": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ElevenLabs returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"ElevenLabs request failed: {exc.reason}") from exc


def normalize_command_text(text: str) -> str:
    return " ".join(text.strip().lower().split())


def clean_device_id(device_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", device_id.strip())
    return cleaned[:64] or "unknown"


def public_device(device_id: str) -> dict[str, Any]:
    device = DEVICES.get(device_id, {})
    pending = PENDING_ACTIONS.get(device_id)
    return {
        "id": device_id,
        "type": device.get("type", "unknown"),
        "model": device.get("model", ""),
        "capabilities": device.get("capabilities", []),
        "endpoints": device.get("endpoints", {}),
        "status": device.get("status", {}),
        "first_seen": device.get("first_seen"),
        "last_seen": device.get("last_seen"),
        "remote_addr": device.get("remote_addr"),
        "user_agent": device.get("user_agent", ""),
        "request_count": device.get("request_count", 0),
        "muted": MUTED_DEVICES.get(device_id, False),
        "pending": pending,
        "pending_events": len(DEVICE_EVENTS.get(device_id, [])),
        "last_command": device.get("last_command"),
        "last_transcript": device.get("last_transcript", ""),
        "last_display_text": device.get("last_display_text", ""),
    }


def touch_device(device_id: str, handler: BaseHTTPRequestHandler | None = None) -> None:
    now = int(time.time())
    device = DEVICES.setdefault(device_id, {
        "id": device_id,
        "first_seen": now,
        "request_count": 0,
    })
    device["last_seen"] = now
    device["request_count"] = int(device.get("request_count", 0)) + 1
    if handler is not None:
        device["remote_addr"] = handler.client_address[0]
        device["user_agent"] = handler.headers.get("User-Agent", "")


def register_device(device_id: str, payload: dict[str, Any], handler: BaseHTTPRequestHandler | None = None) -> dict[str, Any]:
    touch_device(device_id, handler)
    device = DEVICES[device_id]

    if "type" in payload:
        device["type"] = str(payload.get("type", "unknown"))[:32]
    if "model" in payload:
        device["model"] = str(payload.get("model", ""))[:80]
    if isinstance(payload.get("capabilities"), list):
        device["capabilities"] = [str(item)[:40] for item in payload["capabilities"][:16]]
    if isinstance(payload.get("endpoints"), dict):
        device["endpoints"] = {
            str(key)[:40]: str(value)[:240]
            for key, value in payload["endpoints"].items()
        }
    if isinstance(payload.get("status"), dict):
        device["status"] = {
            str(key)[:40]: value
            for key, value in payload["status"].items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }

    return public_device(device_id)


def update_device_result(device_id: str, response: dict[str, Any]) -> None:
    device = DEVICES.setdefault(device_id, {
        "id": device_id,
        "first_seen": int(time.time()),
        "request_count": 0,
    })
    device["last_command"] = response.get("command")
    device["last_transcript"] = response.get("transcript", "")
    device["last_display_text"] = response.get("display_text", "")


def device_ip(device: dict[str, Any]) -> str:
    status = device.get("status", {})
    if isinstance(status, dict) and status.get("ip"):
        return str(status["ip"])
    if device.get("remote_addr"):
        return str(device["remote_addr"])
    endpoints = device.get("endpoints", {})
    if isinstance(endpoints, dict):
        for value in endpoints.values():
            parsed = urlparse(str(value))
            if parsed.hostname:
                return parsed.hostname
    return "unknown"


def device_display_name(device_id: str, device: dict[str, Any]) -> str:
    model = str(device.get("model", "")).strip()
    if model:
        return model
    device_type = str(device.get("type", "")).strip()
    if device_type and device_type != "unknown":
        return f"{device_type} {device_id}"
    return device_id


def status_devices() -> list[dict[str, str]]:
    return [
        {
            "id": device_id,
            "name": device_display_name(device_id, DEVICES[device_id]),
            "type": str(DEVICES[device_id].get("type", "unknown")),
            "ip": device_ip(DEVICES[device_id]),
        }
        for device_id in sorted(DEVICES)
    ]


def device_ping_url(device: dict[str, Any]) -> str | None:
    endpoints = device.get("endpoints", {})
    if isinstance(endpoints, dict):
        for key in ("root", "health", "capture", "stream"):
            value = endpoints.get(key)
            if value:
                return str(value)
        for value in endpoints.values():
            if value:
                return str(value)
    return None


def http_device_online(url: str) -> tuple[bool, str]:
    try:
        request = Request(url, method="GET", headers={"User-Agent": "SpokenCommandServer/0.1"})
        with urlopen(request, timeout=DEVICE_PING_TIMEOUT_SECONDS) as response:
            return response.status < 500, f"http {response.status}"
    except Exception as exc:
        return False, str(exc)


def recent_device_online(device: dict[str, Any]) -> tuple[bool, str]:
    last_seen = device.get("last_seen")
    if not isinstance(last_seen, (int, float)):
        return False, "never seen"
    age = max(0, int(time.time() - last_seen))
    if age <= DEVICE_STALE_SECONDS:
        return True, f"seen {age}s ago"
    return False, f"stale {age}s"


def ping_device(device_id: str, device: dict[str, Any]) -> dict[str, Any]:
    url = device_ping_url(device)
    if url:
        online, detail = http_device_online(url)
        method = "http"
    else:
        online, detail = recent_device_online(device)
        method = "last_seen"
    return {
        "id": device_id,
        "name": device_display_name(device_id, device),
        "type": str(device.get("type", "unknown")),
        "ip": device_ip(device),
        "online": online,
        "method": method,
        "detail": detail,
    }


def dashboard_device(device_id: str) -> dict[str, Any]:
    public = public_device(device_id)
    raw = DEVICES.get(device_id, {})
    online, detail = recent_device_online(raw)
    last_seen = public.get("last_seen")
    age_seconds = None
    if isinstance(last_seen, (int, float)):
        age_seconds = max(0, int(time.time() - last_seen))
    public["online"] = online
    public["online_detail"] = detail
    public["age_seconds"] = age_seconds
    public["ip"] = device_ip(raw)
    public["display_name"] = device_display_name(device_id, raw)
    return public


def dashboard_snapshot() -> dict[str, Any]:
    devices = [dashboard_device(device_id) for device_id in sorted(DEVICES)]
    online_count = sum(1 for device in devices if device.get("online"))
    device_types: dict[str, int] = {}
    capability_counts: dict[str, int] = {}
    for device in devices:
        device_type = str(device.get("type", "unknown"))
        device_types[device_type] = device_types.get(device_type, 0) + 1
        for capability in device.get("capabilities", []):
            key = str(capability)
            capability_counts[key] = capability_counts.get(key, 0) + 1

    return {
        "server": {
            "host": HOST,
            "port": PORT,
            "started_at": SERVER_STARTED_AT,
            "uptime_seconds": int(time.time() - SERVER_STARTED_AT),
            "global_muted": GLOBAL_MUTED,
            "device_stale_seconds": DEVICE_STALE_SECONDS,
        },
        "summary": {
            "device_count": len(devices),
            "online_count": online_count,
            "offline_count": len(devices) - online_count,
            "pending_event_count": sum(int(device.get("pending_events", 0)) for device in devices),
            "recent_command_count": len(RECENT_COMMANDS),
            "recent_button_event_count": len(RECENT_BUTTON_EVENTS),
            "device_types": device_types,
            "capabilities": capability_counts,
        },
        "devices": devices,
        "recent_commands": RECENT_COMMANDS[-20:],
        "recent_button_events": RECENT_BUTTON_EVENTS[-20:],
    }


def remove_device(device_id: str) -> None:
    DEVICES.pop(device_id, None)
    DEVICE_EVENTS.pop(device_id, None)
    MUTED_DEVICES.pop(device_id, None)
    PENDING_ACTIONS.pop(device_id, None)


def record_button_event(device_id: str, payload: dict[str, Any], handler: BaseHTTPRequestHandler | None = None) -> dict[str, Any]:
    touch_device(device_id, handler)
    event = {
        "device_id": device_id,
        "received_at": int(time.time()),
        "event": str(payload.get("event", "click"))[:32],
        "button": str(payload.get("button", "button"))[:32],
        "gpio": payload.get("gpio"),
        "active_low": payload.get("active_low"),
        "click_count": payload.get("click_count"),
        "uptime_ms": payload.get("uptime_ms"),
        "remote_addr": DEVICES.get(device_id, {}).get("remote_addr"),
    }
    RECENT_BUTTON_EVENTS.append(event)
    del RECENT_BUTTON_EVENTS[:-100]

    device = DEVICES.get(device_id, {})
    status = dict(device.get("status", {})) if isinstance(device.get("status"), dict) else {}
    status["last_button_event"] = event["event"]
    status["last_button"] = event["button"]
    if isinstance(event["click_count"], (int, float)):
        status["click_count"] = event["click_count"]
    device["status"] = status

    print(
        "Button event: "
        f"device={device_id} button={event['button']} event={event['event']} "
        f"count={event['click_count']} gpio={event['gpio']} remote={event['remote_addr']}"
    )
    return event


def enqueue_device_event(device_id: str, event_type: str, display_text: str, tone: str = "success",
                         source_device_id: str | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    event = {
        "id": uuid.uuid4().hex,
        "type": event_type,
        "display_text": display_text,
        "tone": tone,
        "created_at": int(time.time()),
    }
    if source_device_id:
        event["source_device_id"] = source_device_id
    if extra:
        for key, value in extra.items():
            if key not in event and isinstance(value, (str, int, float, bool)) and value is not None:
                event[key] = value
    DEVICE_EVENTS.setdefault(device_id, []).append(event)
    return event


def pop_device_events(device_id: str, limit: int = 1) -> list[dict[str, Any]]:
    events = DEVICE_EVENTS.get(device_id, [])
    popped = events[:limit]
    remaining = events[limit:]
    if remaining:
        DEVICE_EVENTS[device_id] = remaining
    else:
        DEVICE_EVENTS.pop(device_id, None)
    return popped


def event_payload_from_body(body: bytes) -> tuple[str, str, str, dict[str, Any]]:
    payload = json.loads(body.decode("utf-8"))
    event_type = str(payload.get("type", "alert"))[:32]
    display_text = str(payload.get("display_text", "Alert"))[:160]
    tone = str(payload.get("tone", "alert"))[:24]
    extra = {
        str(key)[:40]: value
        for key, value in payload.items()
        if key not in {"id", "type", "display_text", "tone", "created_at", "source_device_id"}
    }
    return event_type, display_text, tone, extra


def base_response(ok: bool, transcript: str, display_text: str, tone: str = "success", command: str | None = None,
                  state: dict[str, Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": ok,
        "transcript": transcript,
        "display_text": display_text,
        "tone": tone,
    }
    if command is not None:
        response["command"] = command
    if state:
        response["state"] = state
    return response


def apply_mute_state(device_id: str, response: dict[str, Any]) -> dict[str, Any]:
    if GLOBAL_MUTED or MUTED_DEVICES.get(device_id, False):
        response["tone"] = "none"
    return response


def parse_duration_seconds(text: str) -> int | None:
    normalized = normalize_command_text(text)
    words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "fifteen": 15,
        "twenty": 20,
        "thirty": 30,
        "forty": 40,
        "forty five": 45,
        "sixty": 60,
    }

    for phrase, value in sorted(words.items(), key=lambda item: len(item[0]), reverse=True):
        normalized = re.sub(rf"\b{re.escape(phrase)}\b", str(value), normalized)

    match = re.search(r"\b(\d+)\s*(second|seconds|sec|secs)\b", normalized)
    if match:
        return int(match.group(1))

    match = re.search(r"\b(\d+)\s*(minute|minutes|min|mins)\b", normalized)
    if match:
        return int(match.group(1)) * 60

    match = re.search(r"\b(\d+)\s*(hour|hours|hr|hrs)\b", normalized)
    if match:
        return int(match.group(1)) * 3600

    match = re.fullmatch(r"\d+", normalized)
    if match:
        return int(normalized) * 60

    return None


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} sec"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''}"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} min"
    minutes, sec = divmod(seconds, 60)
    return f"{minutes} min {sec} sec"


def timer_response(transcript: str, device_id: str, duration_text: str) -> dict[str, Any]:
    seconds = parse_duration_seconds(duration_text)
    if seconds is None or seconds <= 0:
        PENDING_ACTIONS[device_id] = {
            "command": "timer",
            "slot": "duration",
            "prompt": "How long should the timer be?",
            "created_at": time.time(),
        }
        return apply_mute_state(device_id, base_response(
            False,
            transcript,
            "How long should the timer be?",
            "error",
            command="timer",
            state={"awaiting": "duration"},
        ))

    return apply_mute_state(device_id, base_response(
        True,
        transcript,
        f"Timer set: {format_duration(seconds)}",
        "success",
        command="timer",
        state={"duration_seconds": seconds, "expires_at": int(time.time() + seconds)},
    ))


def handle_pending_action(device_id: str, text: str, normalized: str) -> dict[str, Any] | None:
    if normalized in {"cancel", "stop", "nevermind", "never mind"}:
        PENDING_ACTIONS.pop(device_id, None)
        return apply_mute_state(device_id, base_response(True, text, "Cancelled.", "success", command="cancel"))

    pending = PENDING_ACTIONS.get(device_id)
    if pending is None:
        return None

    if pending.get("command") == "timer" and pending.get("slot") == "duration":
        response = timer_response(text, device_id, text)
        if response.get("ok"):
            PENDING_ACTIONS.pop(device_id, None)
        return response

    PENDING_ACTIONS.pop(device_id, None)
    return apply_mute_state(device_id, base_response(False, text, "I lost that request.", "error"))


def handle_mute(text: str, device_id: str, remainder: str) -> dict[str, Any]:
    global GLOBAL_MUTED
    if normalize_command_text(remainder) in {"all", "everyone", "everything"}:
        GLOBAL_MUTED = True
        return base_response(True, text, "All devices muted.", "none", command="mute_all", state={"global_muted": True})
    MUTED_DEVICES[device_id] = True
    return base_response(True, text, "Muted.", "none", command="mute", state={"muted": True, "global_muted": GLOBAL_MUTED})


def handle_unmute(text: str, device_id: str, remainder: str) -> dict[str, Any]:
    global GLOBAL_MUTED
    if normalize_command_text(remainder) in {"all", "everyone", "everything"}:
        GLOBAL_MUTED = False
        MUTED_DEVICES.clear()
        return base_response(True, text, "All devices unmuted.", "success", command="unmute_all", state={"global_muted": False})
    MUTED_DEVICES[device_id] = False
    return base_response(True, text, "Unmuted.", "success", command="unmute", state={"muted": False, "global_muted": GLOBAL_MUTED})


def handle_test(text: str, device_id: str, _remainder: str) -> dict[str, Any]:
    return apply_mute_state(device_id, base_response(True, text, "Ready.", "success", command="test"))


def handle_help(text: str, device_id: str, _remainder: str) -> dict[str, Any]:
    return apply_mute_state(device_id, base_response(
        True,
        text,
        "Commands: test, status, list devices, ping, mute, broadcast, timer.",
        "success",
        command="help",
    ))


def handle_status(text: str, device_id: str, _remainder: str) -> dict[str, Any]:
    muted = MUTED_DEVICES.get(device_id, False)
    pending = PENDING_ACTIONS.get(device_id)
    status = "Server online. "
    if GLOBAL_MUTED:
        status += "All devices muted."
    else:
        status += "Muted." if muted else "Sound on."
    if pending:
        status += f" Awaiting {pending.get('slot', 'input')}."
    return apply_mute_state(device_id, base_response(
        True,
        text,
        status,
        "success",
        command="status",
        state={"muted": muted, "global_muted": GLOBAL_MUTED, "pending": pending is not None},
    ))


def handle_list_devices(text: str, device_id: str, _remainder: str) -> dict[str, Any]:
    devices = status_devices()
    if devices:
        lines = [
            f"{device['name']}: {device['ip']}"
            for device in devices[:4]
        ]
        display_text = "Devices:\n" + "\n".join(lines)
        if len(devices) > 4:
            display_text += f"\n+{len(devices) - 4} more"
    else:
        display_text = "No devices registered."
    return apply_mute_state(device_id, base_response(
        True,
        text,
        display_text,
        "success",
        command="list_devices",
        state={"devices": devices},
    ))


def handle_ping(text: str, device_id: str, _remainder: str) -> dict[str, Any]:
    results = [
        ping_device(candidate_id, dict(DEVICES[candidate_id]))
        for candidate_id in sorted(DEVICES)
    ]
    removed = [result for result in results if not result["online"]]
    for result in removed:
        remove_device(result["id"])

    online_count = len(results) - len(removed)
    if results:
        display_text = f"Ping complete. {online_count} online, {len(removed)} removed."
    else:
        display_text = "Ping complete. No devices registered."

    return apply_mute_state(device_id, base_response(
        True,
        text,
        display_text,
        "success" if not removed else "error",
        command="ping",
        state={
            "online_count": online_count,
            "removed_count": len(removed),
            "results": results,
            "removed": removed,
            "devices": status_devices(),
        },
    ))


def handle_cancel(text: str, device_id: str, _remainder: str) -> dict[str, Any]:
    had_pending = device_id in PENDING_ACTIONS
    PENDING_ACTIONS.pop(device_id, None)
    return apply_mute_state(device_id, base_response(
        True,
        text,
        "Cancelled." if had_pending else "Nothing to cancel.",
        "success",
        command="cancel",
    ))


def handle_repeat(text: str, device_id: str, remainder: str) -> dict[str, Any]:
    display_text = remainder.strip()
    return apply_mute_state(device_id, base_response(
        bool(display_text),
        text,
        display_text or "Nothing to repeat.",
        "success" if display_text else "error",
        command="repeat",
    ))


def handle_timer(text: str, device_id: str, remainder: str) -> dict[str, Any]:
    duration_text = remainder.strip()
    if not duration_text:
        PENDING_ACTIONS[device_id] = {
            "command": "timer",
            "slot": "duration",
            "prompt": "How long should the timer be?",
            "created_at": time.time(),
        }
        return apply_mute_state(device_id, base_response(
            True,
            text,
            "How long should the timer be?",
            "success",
            command="timer",
            state={"awaiting": "duration"},
        ))
    return timer_response(text, device_id, duration_text)


def handle_alert(text: str, device_id: str, remainder: str) -> dict[str, Any]:
    normalized = normalize_command_text(f"{text} {remainder}")
    message = "Alert"
    target_ids: list[str]

    if "all devices" in normalized or "everyone" in normalized or "broadcast" in normalized:
        target_ids = sorted(DEVICES.keys())
        if device_id not in target_ids:
            target_ids.append(device_id)
    else:
        target_ids = [device_id]

    if remainder:
        cleaned = re.sub(r"\b(on|to)\s+all\s+devices\b", "", remainder, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"\ball\s+devices\b", "", cleaned, flags=re.IGNORECASE).strip()
        if cleaned:
            message = cleaned[:80]

    for target_id in target_ids:
        enqueue_device_event(target_id, "alert", message, "alert", source_device_id=device_id)

    return apply_mute_state(device_id, base_response(
        True,
        text,
        f"Alert sent to {len(target_ids)} device{'s' if len(target_ids) != 1 else ''}.",
        "success",
        command="alert",
        state={"target_count": len(target_ids), "targets": target_ids},
    ))


def event_capable_device_ids() -> list[str]:
    return [
        candidate_id
        for candidate_id in sorted(DEVICES)
        if not device_matches_type(candidate_id, DEVICES[candidate_id], "camera")
    ]


def handle_broadcast(text: str, device_id: str, remainder: str) -> dict[str, Any]:
    message = remainder.strip()
    if not message:
        return apply_mute_state(device_id, base_response(
            False,
            text,
            "What should I broadcast?",
            "error",
            command="broadcast",
        ))

    target_ids = event_capable_device_ids()
    for target_id in target_ids:
        enqueue_device_event(target_id, "alert", message[:160], "none", source_device_id=device_id)

    return apply_mute_state(device_id, base_response(
        True,
        text,
        f"Broadcast sent to {len(target_ids)} device{'s' if len(target_ids) != 1 else ''}.",
        "success",
        command="broadcast",
        state={"target_count": len(target_ids), "targets": target_ids, "message": message[:160]},
    ))


def device_matches_type(device_id: str, device: dict[str, Any], wanted_type: str) -> bool:
    if device.get("type") == wanted_type:
        return True
    if wanted_type == "display" and "display" in device_id:
        return True
    if wanted_type == "camera" and ("camera" in device_id or "cam" in device_id):
        return True
    return False


def first_device_id(wanted_type: str) -> str | None:
    for candidate_id in sorted(DEVICES):
        if device_matches_type(candidate_id, DEVICES[candidate_id], wanted_type):
            return candidate_id
    return None


def handle_camera_view(text: str, device_id: str, _remainder: str) -> dict[str, Any]:
    camera_id = first_device_id("camera")
    display_id = first_device_id("display")
    if not camera_id or not display_id:
        return apply_mute_state(device_id, base_response(
            False,
            text,
            "Camera or display not found.",
            "error",
            command="camera_view",
        ))

    camera = DEVICES[camera_id]
    endpoints = camera.get("endpoints", {})
    capture_url = endpoints.get("capture") if isinstance(endpoints, dict) else None
    if not capture_url:
        return apply_mute_state(device_id, base_response(
            False,
            text,
            "Camera capture URL missing.",
            "error",
            command="camera_view",
            state={"camera_id": camera_id},
        ))

    enqueue_device_event(
        display_id,
        "camera_view",
        "Camera",
        "none",
        source_device_id=device_id,
        extra={"camera_id": camera_id, "capture_url": capture_url},
    )
    return apply_mute_state(device_id, base_response(
        True,
        text,
        "Showing camera.",
        "success",
        command="camera_view",
        state={"camera_id": camera_id, "display_id": display_id},
    ))


COMMANDS: tuple[Command, ...] = (
    Command("mute", ("mute",), "Disable response tones for this device.", handle_mute),
    Command("unmute", ("unmute",), "Enable response tones for this device.", handle_unmute),
    Command("test", ("test",), "Check that the command server is ready.", handle_test),
    Command("ping", ("ping", "ping devices", "check devices", "check all devices"), "Check known devices and remove offline entries.", handle_ping),
    Command("help", ("help", "commands", "what can you do"), "Show available commands.", handle_help),
    Command("status", ("status", "server status"), "Show server/device state.", handle_status),
    Command("list_devices", ("list devices", "devices", "device list", "show devices"), "Show known devices and IP addresses.", handle_list_devices),
    Command("cancel", ("cancel", "stop", "nevermind", "never mind"), "Cancel a pending command.", handle_cancel),
    Command("repeat", ("repeat", "say"), "Display the spoken suffix.", handle_repeat),
    Command("timer", ("timer", "set timer", "set a timer", "start timer", "start a timer"), "Set a timer.", handle_timer),
    Command("alert", ("alert", "show alert", "show an alert", "send alert", "send an alert", "broadcast alert"), "Show an alert on one or more devices.", handle_alert),
    Command("broadcast", ("broadcast",), "Broadcast text to all known devices.", handle_broadcast),
    Command("camera_view", ("show camera", "show the camera", "show security cam", "show the security cam", "show security camera", "show the security camera", "display camera", "display the camera", "display security cam", "display the security cam", "display security camera", "display the security camera"), "Show a camera frame on a display.", handle_camera_view),
)


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Device Dashboard</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #18202a;
      --muted: #687383;
      --line: #dce2ea;
      --good: #167a46;
      --bad: #b42318;
      --warn: #9a6700;
      --accent: #2458a6;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #111418;
        --panel: #1a1f26;
        --text: #edf1f7;
        --muted: #9aa5b5;
        --line: #303946;
        --good: #4ec98a;
        --bad: #ff7b72;
        --warn: #d9a441;
        --accent: #78a8ff;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.4 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 22px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 1;
    }
    h1 {
      margin: 0;
      font-size: 20px;
      font-weight: 650;
    }
    main {
      max-width: 1280px;
      margin: 0 auto;
      padding: 20px;
    }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 12px;
      color: var(--muted);
      flex-wrap: wrap;
    }
    button {
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--text);
      height: 34px;
      padding: 0 12px;
      border-radius: 6px;
      cursor: pointer;
    }
    button:hover { border-color: var(--accent); }
    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .stat, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .stat {
      padding: 14px;
      min-height: 82px;
    }
    .stat .label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
    }
    .stat .value {
      display: block;
      margin-top: 6px;
      font-size: 26px;
      font-weight: 700;
    }
    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 360px;
      gap: 18px;
      align-items: start;
    }
    .panel {
      overflow: hidden;
    }
    .panel h2 {
      margin: 0;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      font-size: 15px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    th, td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      text-transform: uppercase;
    }
    tr:last-child td { border-bottom: 0; }
    .device-id {
      font-weight: 650;
      word-break: break-word;
    }
    .meta {
      color: var(--muted);
      font-size: 12px;
      margin-top: 3px;
    }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      white-space: nowrap;
    }
    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--bad);
    }
    .online .dot { background: var(--good); }
    .chips {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }
    .chip {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 8px;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .links {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    a {
      color: var(--accent);
      text-decoration: none;
    }
    a:hover { text-decoration: underline; }
    .events {
      display: grid;
      gap: 12px;
      padding: 12px;
    }
    .event {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
    }
    .empty {
      padding: 18px;
      color: var(--muted);
    }
    code {
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 12px;
      word-break: break-word;
    }
    @media (max-width: 900px) {
      header { align-items: flex-start; flex-direction: column; }
      .grid { grid-template-columns: 1fr; }
      table, thead, tbody, th, td, tr { display: block; }
      thead { display: none; }
      tr { border-bottom: 1px solid var(--line); padding: 10px 0; }
      td { border-bottom: 0; padding: 6px 12px; }
      td::before {
        content: attr(data-label);
        display: block;
        color: var(--muted);
        font-size: 11px;
        text-transform: uppercase;
        margin-bottom: 2px;
      }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Device Dashboard</h1>
      <div class="meta" id="serverMeta">Loading server state</div>
    </div>
    <div class="toolbar">
      <span id="refreshState">Waiting for first refresh</span>
      <button id="refreshButton" type="button">Refresh</button>
    </div>
  </header>
  <main>
    <section class="stats" id="stats"></section>
    <section class="grid">
      <div class="panel">
        <h2>Devices</h2>
        <div id="devices"></div>
      </div>
      <div>
        <div class="panel">
          <h2>Recent Commands</h2>
          <div class="events" id="commands"></div>
        </div>
        <div style="height:18px"></div>
        <div class="panel">
          <h2>Button Events</h2>
          <div class="events" id="buttonEvents"></div>
        </div>
      </div>
    </section>
  </main>
  <script>
    const state = {
      refreshMs: 5000,
      timer: null,
    };

    function text(value) {
      if (value === null || value === undefined || value === "") return "-";
      return String(value);
    }

    function age(value) {
      if (value === null || value === undefined) return "-";
      if (value < 60) return `${value}s ago`;
      if (value < 3600) return `${Math.floor(value / 60)}m ago`;
      return `${Math.floor(value / 3600)}h ago`;
    }

    function uptime(seconds) {
      if (!Number.isFinite(seconds)) return "-";
      const h = Math.floor(seconds / 3600);
      const m = Math.floor((seconds % 3600) / 60);
      const s = seconds % 60;
      if (h) return `${h}h ${m}m`;
      if (m) return `${m}m ${s}s`;
      return `${s}s`;
    }

    function el(tag, className, content) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (content !== undefined) node.textContent = content;
      return node;
    }

    function renderStats(data) {
      const stats = document.getElementById("stats");
      stats.replaceChildren();
      const items = [
        ["Devices", data.summary.device_count],
        ["Online", data.summary.online_count],
        ["Offline", data.summary.offline_count],
        ["Pending Events", data.summary.pending_event_count],
        ["Commands", data.summary.recent_command_count],
        ["Buttons", data.summary.recent_button_event_count],
      ];
      for (const [label, value] of items) {
        const card = el("div", "stat");
        card.append(el("div", "label", label));
        card.append(el("span", "value", value));
        stats.append(card);
      }
    }

    function endpointLinks(endpoints) {
      const wrap = el("div", "links");
      const entries = Object.entries(endpoints || {});
      if (!entries.length) {
        wrap.append(el("span", "meta", "-"));
        return wrap;
      }
      for (const [name, url] of entries) {
        const link = el("a", "", name);
        link.href = url;
        link.target = "_blank";
        link.rel = "noreferrer";
        wrap.append(link);
      }
      return wrap;
    }

    function chips(items) {
      const wrap = el("div", "chips");
      if (!items || !items.length) {
        wrap.append(el("span", "meta", "-"));
        return wrap;
      }
      for (const item of items) wrap.append(el("span", "chip", item));
      return wrap;
    }

    function renderDevices(devices) {
      const root = document.getElementById("devices");
      root.replaceChildren();
      if (!devices.length) {
        root.append(el("div", "empty", "No devices have registered yet."));
        return;
      }
      const table = el("table");
      const thead = document.createElement("thead");
      thead.innerHTML = "<tr><th>Device</th><th>Status</th><th>Type</th><th>Capabilities</th><th>Endpoints</th><th>Last Result</th></tr>";
      const tbody = document.createElement("tbody");
      for (const device of devices) {
        const tr = document.createElement("tr");
        const tdDevice = document.createElement("td");
        tdDevice.dataset.label = "Device";
        tdDevice.append(el("div", "device-id", device.id));
        tdDevice.append(el("div", "meta", `${text(device.display_name)} | ${text(device.ip)}`));

        const tdStatus = document.createElement("td");
        tdStatus.dataset.label = "Status";
        const status = el("span", `status ${device.online ? "online" : ""}`);
        status.append(el("span", "dot"));
        status.append(el("span", "", device.online ? "Online" : "Offline"));
        tdStatus.append(status);
        tdStatus.append(el("div", "meta", `${text(device.online_detail)} | ${age(device.age_seconds)}`));
        if (device.muted) tdStatus.append(el("div", "meta", "Muted"));
        if (device.pending_events) tdStatus.append(el("div", "meta", `${device.pending_events} event(s) queued`));

        const tdType = document.createElement("td");
        tdType.dataset.label = "Type";
        tdType.textContent = text(device.type);
        tdType.append(el("div", "meta", text(device.model)));

        const tdCaps = document.createElement("td");
        tdCaps.dataset.label = "Capabilities";
        tdCaps.append(chips(device.capabilities));

        const tdEndpoints = document.createElement("td");
        tdEndpoints.dataset.label = "Endpoints";
        tdEndpoints.append(endpointLinks(device.endpoints));

        const tdLast = document.createElement("td");
        tdLast.dataset.label = "Last Result";
        tdLast.append(el("div", "", text(device.last_command)));
        tdLast.append(el("div", "meta", text(device.last_display_text)));

        tr.append(tdDevice, tdStatus, tdType, tdCaps, tdEndpoints, tdLast);
        tbody.append(tr);
      }
      table.append(thead, tbody);
      root.append(table);
    }

    function renderEvents(id, events, emptyText, mapper) {
      const root = document.getElementById(id);
      root.replaceChildren();
      if (!events.length) {
        root.append(el("div", "empty", emptyText));
        return;
      }
      for (const event of [...events].reverse()) {
        const item = el("div", "event");
        mapper(item, event);
        root.append(item);
      }
    }

    function render(data) {
      document.getElementById("serverMeta").textContent =
        `Listening on ${data.server.host}:${data.server.port} | uptime ${uptime(data.server.uptime_seconds)} | stale after ${data.server.device_stale_seconds}s`;
      renderStats(data);
      renderDevices(data.devices);
      renderEvents("commands", data.recent_commands, "No commands recorded.", (item, command) => {
        item.append(el("div", "device-id", text(command.command)));
        item.append(el("div", "meta", `${text(command.device_id)} | ${text(command.duration_ms)} ms | tone ${text(command.tone)}`));
        item.append(el("div", "", text(command.display_text)));
      });
      renderEvents("buttonEvents", data.recent_button_events, "No button events recorded.", (item, event) => {
        item.append(el("div", "device-id", `${text(event.device_id)} ${text(event.event)}`));
        item.append(el("div", "meta", `button ${text(event.button)} | gpio ${text(event.gpio)} | count ${text(event.click_count)}`));
      });
      document.getElementById("refreshState").textContent = `Updated ${new Date().toLocaleTimeString()}`;
    }

    async function refresh() {
      try {
        const response = await fetch("/dashboard-data", {cache: "no-store"});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        render(await response.json());
      } catch (error) {
        document.getElementById("refreshState").textContent = `Refresh failed: ${error.message}`;
      }
    }

    document.getElementById("refreshButton").addEventListener("click", refresh);
    refresh();
    state.timer = setInterval(refresh, state.refreshMs);
  </script>
</body>
</html>
"""


def dispatch_command(text: str, device_id: str) -> dict[str, Any] | None:
    normalized = normalize_command_text(text)
    for command in COMMANDS:
        for alias in sorted(command.aliases, key=len, reverse=True):
            if normalized == alias:
                return command.handler(text, device_id, "")
            if normalized.startswith(f"{alias} "):
                remainder = text[len(alias):].strip()
                return command.handler(text, device_id, remainder)
    return None


def command_response(transcript_text: str, device_id: str = "unknown") -> dict[str, Any]:
    with STATE_LOCK:
        text = transcript_text.strip()
        normalized = normalize_command_text(text)

        if not text:
            return apply_mute_state(device_id, base_response(False, "", "No speech heard.", "error"))

        immediate_commands = {"mute", "mute all", "unmute", "unmute all", "status", "server status", "list devices", "devices", "device list", "show devices", "ping", "ping devices", "check devices", "check all devices", "help", "commands", "what can you do", "cancel", "stop", "nevermind", "never mind"}
        if normalized in immediate_commands or normalized.startswith("broadcast "):
            command = dispatch_command(text, device_id)
            if command is not None:
                return command

        pending_response = handle_pending_action(device_id, text, normalized)
        if pending_response is not None:
            return pending_response

        command = dispatch_command(text, device_id)
        if command is not None:
            return command

        return apply_mute_state(device_id, base_response(True, text, f"Heard: {text}", "success", command="unknown"))


class CommandHandler(BaseHTTPRequestHandler):
    server_version = "SpokenCommandServer/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path in ("/", "/dashboard"):
            html_response(self, 200, DASHBOARD_HTML)
            return
        if parsed.path == "/dashboard-data":
            with STATE_LOCK:
                payload = dashboard_snapshot()
            json_response(self, 200, payload)
            return
        if parsed.path == "/health":
            json_response(self, 200, {"ok": True, "service": "spoken-command-server"})
            return
        if parsed.path == "/devices":
            with STATE_LOCK:
                devices = [public_device(device_id) for device_id in sorted(DEVICES)]
            json_response(self, 200, {"devices": devices})
            return
        if parsed.path.startswith("/devices/") and parsed.path.endswith("/events"):
            device_id = clean_device_id(parsed.path.removeprefix("/devices/").removesuffix("/events"))
            with STATE_LOCK:
                touch_device(device_id, self)
                events = pop_device_events(device_id)
            json_response(self, 200, {"device_id": device_id, "events": events})
            return
        if parsed.path.startswith("/devices/"):
            device_id = clean_device_id(parsed.path.removeprefix("/devices/"))
            with STATE_LOCK:
                if device_id not in DEVICES:
                    json_response(self, 404, {"error": "device not found"})
                    return
                device = public_device(device_id)
            json_response(self, 200, {"device": device})
            return
        if parsed.path == "/commands/recent":
            device_filter = query.get("device_id", [None])[0]
            limit_text = query.get("limit", ["20"])[0]
            try:
                limit = max(1, min(int(limit_text), 100))
            except ValueError:
                limit = 20
            with STATE_LOCK:
                commands = RECENT_COMMANDS
                if device_filter:
                    device_filter = clean_device_id(device_filter)
                    commands = [command for command in commands if command.get("device_id") == device_filter]
                commands = commands[-limit:]
            json_response(self, 200, {"commands": commands})
            return
        if parsed.path == "/button-events/recent":
            device_filter = query.get("device_id", [None])[0]
            limit_text = query.get("limit", ["20"])[0]
            try:
                limit = max(1, min(int(limit_text), 100))
            except ValueError:
                limit = 20
            with STATE_LOCK:
                events = RECENT_BUTTON_EVENTS
                if device_filter:
                    device_filter = clean_device_id(device_filter)
                    events = [event for event in events if event.get("device_id") == device_filter]
                events = events[-limit:]
            json_response(self, 200, {"button_events": events})
            return
        json_response(self, 404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/devices/events":
            try:
                body = read_request_body(self)
                event_type, display_text, tone, extra = event_payload_from_body(body)
                with STATE_LOCK:
                    target_ids = sorted(DEVICES.keys())
                    events = {
                        device_id: enqueue_device_event(device_id, event_type, display_text, tone, source_device_id="server", extra=extra)
                        for device_id in target_ids
                    }
                json_response(self, 200, {"ok": True, "target_count": len(target_ids), "targets": target_ids, "events": events})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path.startswith("/devices/") and parsed.path.endswith("/register"):
            device_id = clean_device_id(parsed.path.removeprefix("/devices/").removesuffix("/register"))
            try:
                body = read_request_body(self)
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("registration body must be a JSON object")
                with STATE_LOCK:
                    device = register_device(device_id, payload, self)
                json_response(self, 200, {"ok": True, "device": device})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path.startswith("/devices/") and parsed.path.endswith("/button"):
            device_id = clean_device_id(parsed.path.removeprefix("/devices/").removesuffix("/button"))
            try:
                body = read_request_body(self)
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("button event body must be a JSON object")
                with STATE_LOCK:
                    event = record_button_event(device_id, payload, self)
                json_response(self, 200, {"ok": True, "device_id": device_id, "button_event": event})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path.startswith("/devices/") and parsed.path.endswith("/events"):
            device_id = clean_device_id(parsed.path.removeprefix("/devices/").removesuffix("/events"))
            try:
                body = read_request_body(self)
                event_type, display_text, tone, extra = event_payload_from_body(body)
                with STATE_LOCK:
                    touch_device(device_id, self)
                    event = enqueue_device_event(device_id, event_type, display_text, tone, source_device_id="server", extra=extra)
                json_response(self, 200, {"ok": True, "device_id": device_id, "event": event})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if self.path != "/audio/command":
            json_response(self, 404, {"error": "not found"})
            return

        try:
            body = read_request_body(self)
            content_type = self.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
            sample_rate = int(self.headers.get("X-Audio-Sample-Rate", "16000"))
            channels = int(self.headers.get("X-Audio-Channels", "1"))
            device_id = clean_device_id(self.headers.get("X-Device-Id", "unknown"))
            with STATE_LOCK:
                touch_device(device_id, self)

            if content_type in ("audio/wav", "audio/x-wav"):
                wav_bytes = body
            elif content_type in ("application/octet-stream", "audio/pcm"):
                wav_bytes = pcm_s16le_to_wav(body, sample_rate, channels)
            else:
                raise ValueError(f"unsupported Content-Type: {content_type}")

            started = time.monotonic()
            transcript = transcribe_with_elevenlabs(wav_bytes)
            device_response = command_response(transcript.get("text", ""), device_id)
            record = {
                "device_id": device_id,
                "received_at": int(time.time()),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "text": device_response["transcript"],
                "display_text": device_response["display_text"],
                "tone": device_response["tone"],
                "command": device_response.get("command"),
                "state": device_response.get("state", {}),
                "muted": MUTED_DEVICES.get(device_id, False),
                "transcript": transcript,
            }
            with STATE_LOCK:
                update_device_result(device_id, device_response)
                RECENT_COMMANDS.append(record)
                del RECENT_COMMANDS[:-100]
            json_response(self, 200, device_response)
        except Exception as exc:
            json_response(self, 400, {
                "ok": False,
                "transcript": "",
                "display_text": "Command failed.",
                "tone": "error",
                "error": str(exc),
            })

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), CommandHandler)
    print(f"Listening on http://{HOST}:{PORT}")
    print("POST audio to /audio/command with ELEVENLABS_API_KEY set in the environment.")
    server.serve_forever()


if __name__ == "__main__":
    main()
