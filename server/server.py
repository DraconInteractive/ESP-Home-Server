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
MEDIA_SNAPSHOT_TTL_SECONDS = float(os.environ.get("COMMAND_SERVER_MEDIA_SNAPSHOT_TTL_SECONDS", "1.0"))
MEDIA_STREAM_IDLE_SECONDS = float(os.environ.get("COMMAND_SERVER_MEDIA_STREAM_IDLE_SECONDS", "3.0"))
MEDIA_STREAM_CHUNK_SIZE = int(os.environ.get("COMMAND_SERVER_MEDIA_STREAM_CHUNK_SIZE", "4096"))
DEVICE_NAMES_PATH = os.environ.get(
    "COMMAND_SERVER_DEVICE_NAMES_PATH",
    os.path.join(os.path.dirname(__file__), "device-names.json"),
)
DEVICE_REGISTRY_PATH = os.environ.get(
    "COMMAND_SERVER_DEVICE_REGISTRY_PATH",
    os.path.join(os.path.dirname(__file__), "device-registry.json"),
)
SERVER_STARTED_AT = int(time.time())

RECENT_COMMANDS: list[dict[str, Any]] = []
RECENT_BUTTON_EVENTS: list[dict[str, Any]] = []
MUTED_DEVICES: dict[str, bool] = {}
GLOBAL_MUTED = False
PENDING_ACTIONS: dict[str, dict[str, Any]] = {}
DEVICES: dict[str, dict[str, Any]] = {}
DEVICE_EVENTS: dict[str, list[dict[str, Any]]] = {}
DEVICE_FRIENDLY_NAMES: dict[str, str] = {}
MEDIA_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
MEDIA_STREAMS: dict[tuple[str, str], "MediaStreamProxy"] = {}
STATE_LOCK = threading.RLock()

STREAM_ENDPOINT_NAMES = {"video", "stream"}
MEDIA_ENDPOINT_NAMES = {"capture", "audio", "video", "stream"}


@dataclass(frozen=True)
class Command:
    name: str
    aliases: tuple[str, ...]
    description: str
    handler: Callable[[str, str, str], dict[str, Any]]


class MediaStreamProxy:
    def __init__(self, device_id: str, endpoint_name: str, url: str):
        self.device_id = device_id
        self.endpoint_name = endpoint_name
        self.url = url
        self.condition = threading.Condition()
        self.chunks: list[tuple[int, bytes]] = []
        self.seq = 0
        self.content_type = "application/octet-stream"
        self.error: str | None = None
        self.thread: threading.Thread | None = None
        self.clients = 0
        self.last_client_left_at = 0.0
        self.stop_requested = False

    def ensure_started(self) -> None:
        with self.condition:
            if self.thread and self.thread.is_alive():
                return
            self.error = None
            self.stop_requested = False
            self.chunks = []
            self.seq = 0
            self.content_type = "application/octet-stream"
            self.thread = threading.Thread(
                target=self._reader,
                name=f"media-{self.device_id}-{self.endpoint_name}",
                daemon=True,
            )
            self.thread.start()

    def _reader(self) -> None:
        try:
            request = Request(self.url, headers={"User-Agent": "SpokenCommandServer/0.1"})
            with urlopen(request, timeout=10) as response:
                with self.condition:
                    self.content_type = response.headers.get("Content-Type", "application/octet-stream")
                    self.condition.notify_all()

                while True:
                    with self.condition:
                        if self.stop_requested:
                            break
                        if self.clients <= 0 and self.last_client_left_at:
                            idle_for = time.monotonic() - self.last_client_left_at
                            if idle_for >= MEDIA_STREAM_IDLE_SECONDS:
                                break

                    chunk = response.read(MEDIA_STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    with self.condition:
                        self.seq += 1
                        self.chunks.append((self.seq, chunk))
                        del self.chunks[:-64]
                        self.condition.notify_all()
        except Exception as exc:
            with self.condition:
                self.error = str(exc)
                self.condition.notify_all()
        finally:
            with self.condition:
                self.thread = None
                self.condition.notify_all()

    def add_client(self) -> None:
        with self.condition:
            self.clients += 1
            self.last_client_left_at = 0.0

    def remove_client(self) -> None:
        with self.condition:
            self.clients = max(0, self.clients - 1)
            if self.clients == 0:
                self.last_client_left_at = time.monotonic()
            self.condition.notify_all()


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


def binary_response(handler: BaseHTTPRequestHandler, status: int, content_type: str, body: bytes,
                    extra_headers: dict[str, str] | None = None) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    if extra_headers:
        for key, value in extra_headers.items():
            handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


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


def clean_friendly_name(name: str) -> str:
    cleaned = " ".join(str(name).strip().split())
    return cleaned[:48]


def load_device_friendly_names() -> None:
    try:
        with open(DEVICE_NAMES_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return
    except Exception as exc:
        print(f"Could not load device friendly names: {exc}")
        return

    if not isinstance(payload, dict):
        return
    devices = payload.get("devices", payload)
    if not isinstance(devices, dict):
        return
    for device_id, name in devices.items():
        cleaned_id = clean_device_id(str(device_id))
        cleaned_name = clean_friendly_name(str(name))
        if cleaned_name:
            DEVICE_FRIENDLY_NAMES[cleaned_id] = cleaned_name


def save_device_friendly_names() -> None:
    directory = os.path.dirname(DEVICE_NAMES_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = {"devices": dict(sorted(DEVICE_FRIENDLY_NAMES.items()))}
    temp_path = f"{DEVICE_NAMES_PATH}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    os.replace(temp_path, DEVICE_NAMES_PATH)


def set_device_friendly_name(device_id: str, name: str) -> dict[str, Any]:
    cleaned_id = clean_device_id(device_id)
    cleaned_name = clean_friendly_name(name)
    if cleaned_name:
        DEVICE_FRIENDLY_NAMES[cleaned_id] = cleaned_name
    else:
        DEVICE_FRIENDLY_NAMES.pop(cleaned_id, None)
    if cleaned_id in DEVICES:
        if cleaned_name:
            DEVICES[cleaned_id]["friendly_name"] = cleaned_name
        else:
            DEVICES[cleaned_id].pop("friendly_name", None)
    save_device_friendly_names()
    save_device_registry()
    return public_device(cleaned_id)


def persisted_device(device_id: str, device: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "friendly_name",
        "type",
        "model",
        "capabilities",
        "endpoints",
        "status",
        "first_seen",
        "last_seen",
        "remote_addr",
        "user_agent",
        "request_count",
        "last_command",
        "last_transcript",
        "last_display_text",
    }
    result = {key: value for key, value in device.items() if key in allowed}
    result["id"] = device_id
    if DEVICE_FRIENDLY_NAMES.get(device_id):
        result["friendly_name"] = DEVICE_FRIENDLY_NAMES[device_id]
    return result


def load_device_registry() -> None:
    try:
        with open(DEVICE_REGISTRY_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return
    except Exception as exc:
        print(f"Could not load device registry: {exc}")
        return

    devices = payload.get("devices") if isinstance(payload, dict) else None
    if not isinstance(devices, dict):
        return

    for raw_device_id, raw_device in devices.items():
        if not isinstance(raw_device, dict):
            continue
        device_id = clean_device_id(str(raw_device_id))
        device = persisted_device(device_id, raw_device)
        device["id"] = device_id
        device["session_seen"] = False
        if DEVICE_FRIENDLY_NAMES.get(device_id):
            device["friendly_name"] = DEVICE_FRIENDLY_NAMES[device_id]
        DEVICES[device_id] = device


def save_device_registry() -> None:
    directory = os.path.dirname(DEVICE_REGISTRY_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    devices = {
        device_id: persisted_device(device_id, DEVICES[device_id])
        for device_id in sorted(DEVICES)
    }
    payload = {"devices": devices}
    temp_path = f"{DEVICE_REGISTRY_PATH}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    os.replace(temp_path, DEVICE_REGISTRY_PATH)


def default_device_metadata(device_id: str) -> dict[str, Any]:
    if device_id.startswith("waveshare-c6-"):
        return {
            "type": "voice-controller",
            "model": "Waveshare ESP32-C6 Touch AMOLED 1.8",
            "capabilities": [
                "microphone",
                "display",
                "touch",
                "speaker",
                "button",
                "imu",
                "battery",
                "command-audio",
                "device-events",
            ],
        }
    return {}


def public_device(device_id: str) -> dict[str, Any]:
    device = DEVICES.get(device_id, {})
    defaults = default_device_metadata(device_id)
    friendly_name = DEVICE_FRIENDLY_NAMES.get(device_id) or str(device.get("friendly_name", "")).strip()
    pending = PENDING_ACTIONS.get(device_id)
    return {
        "id": device_id,
        "friendly_name": friendly_name,
        "type": device.get("type") or defaults.get("type", "unknown"),
        "model": device.get("model") or defaults.get("model", ""),
        "capabilities": device.get("capabilities") or defaults.get("capabilities", []),
        "endpoints": device.get("endpoints", {}),
        "status": device.get("status", {}),
        "first_seen": device.get("first_seen"),
        "last_seen": device.get("last_seen"),
        "remote_addr": device.get("remote_addr"),
        "user_agent": device.get("user_agent", ""),
        "request_count": device.get("request_count", 0),
        "session_seen": bool(device.get("session_seen", False)),
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
    for key, value in default_device_metadata(device_id).items():
        device.setdefault(key, value)
    if DEVICE_FRIENDLY_NAMES.get(device_id):
        device["friendly_name"] = DEVICE_FRIENDLY_NAMES[device_id]
    device["session_seen"] = True
    device["last_seen"] = now
    device["request_count"] = int(device.get("request_count", 0)) + 1
    if handler is not None:
        device["remote_addr"] = handler.client_address[0]
        device["user_agent"] = handler.headers.get("User-Agent", "")
    save_device_registry()


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

    save_device_registry()
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
    save_device_registry()


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
    friendly_name = DEVICE_FRIENDLY_NAMES.get(device_id) or str(device.get("friendly_name", "")).strip()
    if friendly_name:
        return friendly_name
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
            "friendly_name": DEVICE_FRIENDLY_NAMES.get(device_id, ""),
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
    if not device.get("session_seen", False):
        return False, "not seen this session"
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
    public["friendly_name"] = DEVICE_FRIENDLY_NAMES.get(device_id, public.get("friendly_name", ""))
    public["proxy_endpoints"] = {
        name: f"/media/{device_id}/{name}"
        for name in sorted((public.get("endpoints") or {}).keys())
        if name in MEDIA_ENDPOINT_NAMES
    }
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
            "media_snapshot_ttl_seconds": MEDIA_SNAPSHOT_TTL_SECONDS,
            "media_stream_idle_seconds": MEDIA_STREAM_IDLE_SECONDS,
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


def media_endpoint_url(device_id: str, endpoint_name: str) -> str | None:
    if endpoint_name not in MEDIA_ENDPOINT_NAMES:
        return None
    device = DEVICES.get(device_id)
    if not device:
        return None
    endpoints = device.get("endpoints", {})
    if not isinstance(endpoints, dict):
        return None
    url = endpoints.get(endpoint_name)
    return str(url) if url else None


def fetch_media_snapshot(device_id: str, endpoint_name: str, url: str) -> dict[str, Any]:
    key = (device_id, endpoint_name)
    now = time.monotonic()
    cached = MEDIA_CACHE.get(key)
    if cached and now - float(cached.get("fetched_at", 0)) <= MEDIA_SNAPSHOT_TTL_SECONDS:
        return cached

    request = Request(url, headers={"User-Agent": "SpokenCommandServer/0.1"})
    with urlopen(request, timeout=10) as response:
        body = response.read(MAX_AUDIO_BYTES)
        if len(body) >= MAX_AUDIO_BYTES:
            raise ValueError("proxied media response reached maximum size")
        result = {
            "fetched_at": now,
            "status": response.status,
            "content_type": response.headers.get("Content-Type", "application/octet-stream"),
            "body": body,
        }
    MEDIA_CACHE[key] = result
    return result


def stream_media_proxy(handler: BaseHTTPRequestHandler, device_id: str, endpoint_name: str, url: str) -> None:
    key = (device_id, endpoint_name)
    with STATE_LOCK:
        proxy = MEDIA_STREAMS.get(key)
        if proxy is None or proxy.url != url:
            proxy = MediaStreamProxy(device_id, endpoint_name, url)
            MEDIA_STREAMS[key] = proxy

    proxy.add_client()
    proxy.ensure_started()
    last_seq = 0
    try:
        with proxy.condition:
            deadline = time.monotonic() + 3
            while proxy.seq == 0 and proxy.error is None and time.monotonic() < deadline:
                proxy.condition.wait(timeout=0.25)
            if proxy.error and proxy.seq == 0:
                json_response(handler, 502, {
                    "error": "media stream upstream failed",
                    "detail": proxy.error,
                    "device_id": device_id,
                    "endpoint": endpoint_name,
                })
                return
            if proxy.seq == 0:
                json_response(handler, 504, {
                    "error": "media stream produced no data",
                    "device_id": device_id,
                    "endpoint": endpoint_name,
                })
                return
            content_type = proxy.content_type

        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Proxied-Device", device_id)
        handler.send_header("X-Proxied-Endpoint", endpoint_name)
        handler.end_headers()

        while True:
            with proxy.condition:
                while proxy.seq <= last_seq and proxy.error is None:
                    if proxy.thread is None:
                        break
                    proxy.condition.wait(timeout=2)
                if proxy.error and proxy.seq <= last_seq:
                    break
                if proxy.thread is None and proxy.seq <= last_seq:
                    break
                chunks = [(seq, chunk) for seq, chunk in proxy.chunks if seq > last_seq]

            for seq, chunk in chunks:
                handler.wfile.write(chunk)
                last_seq = seq
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        proxy.remove_client()


def proxy_media(handler: BaseHTTPRequestHandler, device_id: str, endpoint_name: str) -> None:
    with STATE_LOCK:
        url = media_endpoint_url(device_id, endpoint_name)
    if not url:
        json_response(handler, 404, {"error": "media endpoint not found", "device_id": device_id, "endpoint": endpoint_name})
        return

    if endpoint_name in STREAM_ENDPOINT_NAMES:
        stream_media_proxy(handler, device_id, endpoint_name, url)
        return

    try:
        media = fetch_media_snapshot(device_id, endpoint_name, url)
        binary_response(
            handler,
            int(media.get("status", 200)),
            str(media.get("content_type", "application/octet-stream")),
            media["body"],
            {"Cache-Control": "no-store", "X-Proxied-Device": device_id, "X-Proxied-Endpoint": endpoint_name},
        )
    except Exception as exc:
        json_response(handler, 502, {"error": "media proxy failed", "detail": str(exc), "device_id": device_id, "endpoint": endpoint_name})


def remove_device(device_id: str) -> None:
    DEVICES.pop(device_id, None)
    DEVICE_EVENTS.pop(device_id, None)
    MUTED_DEVICES.pop(device_id, None)
    PENDING_ACTIONS.pop(device_id, None)
    DEVICE_FRIENDLY_NAMES.pop(device_id, None)
    for key in list(MEDIA_CACHE):
        if key[0] == device_id:
            MEDIA_CACHE.pop(key, None)
    for key, proxy in list(MEDIA_STREAMS.items()):
        if key[0] == device_id:
            with proxy.condition:
                proxy.stop_requested = True
                proxy.condition.notify_all()
            MEDIA_STREAMS.pop(key, None)
    save_device_friendly_names()
    save_device_registry()


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
    save_device_registry()

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


def handle_status(text: str, device_id: str, remainder: str) -> dict[str, Any]:
    target_id = device_id
    if remainder.strip():
        target_id = resolve_device_reference(remainder) or ""
        if not target_id:
            return apply_mute_state(device_id, base_response(
                False,
                text,
                "Device not found.",
                "error",
                command="status",
                state={"query": remainder.strip()},
            ))

    target = DEVICES.get(target_id)
    if target is None:
        return apply_mute_state(device_id, base_response(
            False,
            text,
            "Device not registered.",
            "error",
            command="status",
            state={"target_id": target_id},
        ))

    online, detail = recent_device_online(target)
    friendly_name = DEVICE_FRIENDLY_NAMES.get(target_id, "")
    display_name = device_display_name(target_id, target)
    pending = PENDING_ACTIONS.get(target_id)
    muted = GLOBAL_MUTED or MUTED_DEVICES.get(target_id, False)
    ip = device_ip(target)
    device_type = str(target.get("type", "unknown"))
    model = str(target.get("model", "")).strip()
    capabilities = target.get("capabilities", [])
    if not isinstance(capabilities, list):
        capabilities = []

    lines = [
        f"{display_name}",
        f"{'Online' if online else 'Offline'}: {detail}",
        f"ID: {target_id}",
        f"IP: {ip}",
        f"Type: {device_type}",
    ]
    if friendly_name:
        lines.insert(1, f"Name: {friendly_name}")
    if model and model != display_name:
        lines.append(f"Model: {model}")
    if capabilities:
        lines.append("Caps: " + ", ".join(str(item) for item in capabilities[:4]))
    lines.append("Sound: muted" if muted else "Sound: on")
    if pending:
        lines.append(f"Awaiting: {pending.get('slot', 'input')}")

    return apply_mute_state(device_id, base_response(
        True,
        text,
        "\n".join(lines),
        "success" if online else "error",
        command="status",
        state={
            "target_id": target_id,
            "friendly_name": friendly_name,
            "display_name": display_name,
            "online": online,
            "online_detail": detail,
            "ip": ip,
            "type": device_type,
            "model": model,
            "capabilities": capabilities,
            "muted": muted,
            "global_muted": GLOBAL_MUTED,
            "pending": pending is not None,
        },
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
    offline = [result for result in results if not result["online"]]

    online_count = len(results) - len(offline)
    if results:
        display_text = f"Ping complete. {online_count} online, {len(offline)} offline."
    else:
        display_text = "Ping complete. No devices registered."

    return apply_mute_state(device_id, base_response(
        True,
        text,
        display_text,
        "success" if not offline else "error",
        command="ping",
        state={
            "online_count": online_count,
            "offline_count": len(offline),
            "results": results,
            "offline": offline,
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
        named_target = resolve_device_reference(remainder) or resolve_device_reference(text)
        target_ids = [named_target or device_id]

    if remainder:
        cleaned = re.sub(r"\b(on|to)\s+all\s+devices\b", "", remainder, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"\ball\s+devices\b", "", cleaned, flags=re.IGNORECASE).strip()
        for target_id in target_ids:
            for name in device_reference_names(target_id, DEVICES.get(target_id, {})):
                cleaned = re.sub(rf"\b(on|to)\s+{re.escape(name)}\b", "", cleaned, flags=re.IGNORECASE).strip()
                cleaned = re.sub(rf"\b{re.escape(name)}\b", "", cleaned, flags=re.IGNORECASE).strip()
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


def device_reference_names(device_id: str, device: dict[str, Any]) -> list[str]:
    names = [device_id]
    friendly_name = DEVICE_FRIENDLY_NAMES.get(device_id) or str(device.get("friendly_name", "")).strip()
    if friendly_name:
        names.append(friendly_name)
    display_name = device_display_name(device_id, device)
    if display_name and display_name not in names:
        names.append(display_name)
    return names


def resolve_device_reference(text: str, wanted_type: str | None = None) -> str | None:
    normalized_text = normalize_command_text(text)
    matches: list[tuple[int, str]] = []
    for candidate_id, candidate in DEVICES.items():
        if wanted_type and not device_matches_type(candidate_id, candidate, wanted_type):
            continue
        for name in device_reference_names(candidate_id, candidate):
            normalized_name = normalize_command_text(name.replace("-", " "))
            if not normalized_name:
                continue
            if re.search(rf"\b{re.escape(normalized_name)}\b", normalized_text):
                matches.append((len(normalized_name), candidate_id))
                break
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


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
    capabilities = device.get("capabilities", [])
    if not isinstance(capabilities, list):
        capabilities = []
    if device.get("type") == wanted_type:
        return True
    if wanted_type == "display" and "display" in capabilities:
        return True
    if wanted_type == "camera" and any(capability in capabilities for capability in ("capture", "video", "stream")):
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


def handle_camera_view(text: str, device_id: str, remainder: str) -> dict[str, Any]:
    camera_id = resolve_device_reference(remainder, "camera") or resolve_device_reference(text, "camera") or first_device_id("camera")
    display_id = resolve_device_reference(remainder, "display") or resolve_device_reference(text, "display") or first_device_id("display")
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
    Command("ping", ("ping", "ping devices", "check devices", "check all devices"), "Check known devices and report offline entries.", handle_ping),
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
    button, input {
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--text);
      height: 34px;
      padding: 0 12px;
      border-radius: 6px;
    }
    button {
      cursor: pointer;
    }
    button.danger {
      border-color: var(--bad);
      color: var(--bad);
    }
    input {
      min-width: 0;
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
    .name-form {
      display: flex;
      gap: 6px;
      margin-top: 8px;
      max-width: 320px;
    }
    .name-form input {
      flex: 1;
      width: 100%;
    }
    .name-form button {
      flex: 0 0 auto;
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
    .modal-backdrop {
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 20px;
      background: rgba(0, 0, 0, 0.45);
      z-index: 10;
    }
    .modal-backdrop.open {
      display: flex;
    }
    .modal {
      width: min(420px, 100%);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      box-shadow: 0 16px 42px rgba(0, 0, 0, 0.28);
    }
    .modal h2 {
      margin: 0 0 8px;
      font-size: 17px;
    }
    .modal-actions {
      display: flex;
      justify-content: flex-end;
      gap: 10px;
      margin-top: 18px;
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
  <div class="modal-backdrop" id="removeModal" role="dialog" aria-modal="true" aria-labelledby="removeTitle">
    <div class="modal">
      <h2 id="removeTitle">Remove Device?</h2>
      <div id="removeMessage">This device will be removed from the server list.</div>
      <div class="meta">It can appear again if it registers with the server later.</div>
      <div class="modal-actions">
        <button id="cancelRemoveButton" type="button">Cancel</button>
        <button id="confirmRemoveButton" class="danger" type="button">Remove</button>
      </div>
    </div>
  </div>
  <script>
    const state = {
      refreshMs: 5000,
      timer: null,
      pendingRemoval: null,
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

    function endpointLinks(deviceId, endpoints, proxyEndpoints) {
      const wrap = el("div", "links");
      const entries = Object.entries(endpoints || {});
      const proxyEntries = Object.entries(proxyEndpoints || {});
      if (!entries.length && !proxyEntries.length) {
        wrap.append(el("span", "meta", "-"));
        return wrap;
      }
      for (const [name, url] of proxyEntries) {
        const link = el("a", "", `proxy ${name}`);
        link.href = url;
        link.target = "_blank";
        link.rel = "noreferrer";
        wrap.append(link);
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

    async function saveFriendlyName(deviceId, input, button) {
      const original = button.textContent;
      button.disabled = true;
      button.textContent = "Saving";
      try {
        const response = await fetch(`/devices/${encodeURIComponent(deviceId)}/friendly-name`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({friendly_name: input.value}),
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        input.blur();
        await refresh();
      } catch (error) {
        button.textContent = "Failed";
        setTimeout(() => { button.textContent = original; button.disabled = false; }, 1200);
        return;
      }
      button.textContent = original;
      button.disabled = false;
    }

    function friendlyNameForm(device) {
      const form = el("form", "name-form");
      const input = document.createElement("input");
      input.type = "text";
      input.maxLength = 48;
      input.placeholder = "Friendly name";
      input.value = device.friendly_name || "";
      input.autocomplete = "off";
      const button = el("button", "", "Save");
      button.type = "submit";
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        saveFriendlyName(device.id, input, button);
      });
      form.append(input, button);
      return form;
    }

    function openRemoveModal(device) {
      state.pendingRemoval = device;
      document.getElementById("removeMessage").textContent =
        `Remove ${text(device.display_name)} (${device.id}) from the server list?`;
      document.getElementById("removeModal").classList.add("open");
    }

    function closeRemoveModal() {
      state.pendingRemoval = null;
      document.getElementById("removeModal").classList.remove("open");
    }

    async function removePendingDevice() {
      const device = state.pendingRemoval;
      if (!device) return;
      const button = document.getElementById("confirmRemoveButton");
      const original = button.textContent;
      button.disabled = true;
      button.textContent = "Removing";
      try {
        const response = await fetch(`/devices/${encodeURIComponent(device.id)}`, {method: "DELETE"});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        closeRemoveModal();
        await refresh();
      } catch (error) {
        button.textContent = "Failed";
        setTimeout(() => { button.textContent = original; button.disabled = false; }, 1200);
        return;
      }
      button.textContent = original;
      button.disabled = false;
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
      thead.innerHTML = "<tr><th>Device</th><th>Status</th><th>Type</th><th>Capabilities</th><th>Endpoints</th><th>Last Result</th><th>Actions</th></tr>";
      const tbody = document.createElement("tbody");
      for (const device of devices) {
        const tr = document.createElement("tr");
        const tdDevice = document.createElement("td");
        tdDevice.dataset.label = "Device";
        tdDevice.append(el("div", "device-id", device.id));
        tdDevice.append(el("div", "meta", `${text(device.display_name)} | ${text(device.ip)}`));
        tdDevice.append(friendlyNameForm(device));

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
        tdEndpoints.append(endpointLinks(device.id, device.endpoints, device.proxy_endpoints));

        const tdLast = document.createElement("td");
        tdLast.dataset.label = "Last Result";
        tdLast.append(el("div", "", text(device.last_command)));
        tdLast.append(el("div", "meta", text(device.last_display_text)));

        const tdActions = document.createElement("td");
        tdActions.dataset.label = "Actions";
        const removeButton = el("button", "danger", "Remove");
        removeButton.type = "button";
        removeButton.addEventListener("click", () => openRemoveModal(device));
        tdActions.append(removeButton);

        tr.append(tdDevice, tdStatus, tdType, tdCaps, tdEndpoints, tdLast, tdActions);
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

    function stateDetails(state) {
      const details = [];
      if (!state || typeof state !== "object") return details;
      if (state.query) details.push(`query: ${state.query}`);
      if (state.target_id) details.push(`target: ${state.target_id}`);
      if (state.display_name) details.push(`target name: ${state.display_name}`);
      if (state.online_detail) details.push(`target status: ${state.online_detail}`);
      return details;
    }

    function render(data) {
      document.getElementById("serverMeta").textContent =
        `Listening on ${data.server.host}:${data.server.port} | uptime ${uptime(data.server.uptime_seconds)} | stale after ${data.server.device_stale_seconds}s`;
      renderStats(data);
      renderDevices(data.devices);
      renderEvents("commands", data.recent_commands, "No commands recorded.", (item, command) => {
        item.append(el("div", "device-id", text(command.command)));
        item.append(el("div", "meta", `${text(command.device_id)} | ${text(command.duration_ms)} ms | tone ${text(command.tone)} | ${command.ok ? "ok" : "failed"}`));
        item.append(el("div", "", `heard: ${text(command.text)}`));
        item.append(el("div", "", text(command.display_text)));
        for (const detail of stateDetails(command.state)) {
          item.append(el("div", "meta", detail));
        }
      });
      renderEvents("buttonEvents", data.recent_button_events, "No button events recorded.", (item, event) => {
        item.append(el("div", "device-id", `${text(event.device_id)} ${text(event.event)}`));
        item.append(el("div", "meta", `button ${text(event.button)} | gpio ${text(event.gpio)} | count ${text(event.click_count)}`));
      });
      document.getElementById("refreshState").textContent = `Updated ${new Date().toLocaleTimeString()}`;
    }

    async function refresh() {
      if (state.pendingRemoval) {
        document.getElementById("refreshState").textContent = "Refresh paused for confirmation";
        return;
      }
      if (document.activeElement && document.activeElement.closest(".name-form")) {
        document.getElementById("refreshState").textContent = "Refresh paused while editing";
        return;
      }
      try {
        const response = await fetch("/dashboard-data", {cache: "no-store"});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        render(await response.json());
      } catch (error) {
        document.getElementById("refreshState").textContent = `Refresh failed: ${error.message}`;
      }
    }

    document.getElementById("refreshButton").addEventListener("click", refresh);
    document.getElementById("cancelRemoveButton").addEventListener("click", closeRemoveModal);
    document.getElementById("confirmRemoveButton").addEventListener("click", removePendingDevice);
    document.getElementById("removeModal").addEventListener("click", (event) => {
      if (event.target.id === "removeModal") closeRemoveModal();
    });
    refresh();
    state.timer = setInterval(refresh, state.refreshMs);
  </script>
</body>
</html>
"""


CAMERAS_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Camera Grid</title>
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
      max-width: 1440px;
      margin: 0 auto;
      padding: 20px;
    }
    button, select {
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--text);
      height: 34px;
      padding: 0 12px;
      border-radius: 6px;
    }
    button { cursor: pointer; }
    button:hover { border-color: var(--accent); }
    a {
      color: var(--accent);
      text-decoration: none;
    }
    a:hover { text-decoration: underline; }
    .toolbar {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      color: var(--muted);
      flex-wrap: wrap;
    }
    .meta {
      color: var(--muted);
      font-size: 12px;
      margin-top: 3px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 14px;
      align-items: start;
    }
    .camera {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }
    .frame {
      position: relative;
      background: #05070a;
      aspect-ratio: 4 / 3;
      display: grid;
      place-items: center;
    }
    .frame img {
      width: 100%;
      height: 100%;
      object-fit: contain;
      display: block;
    }
    .frame .empty {
      color: #c5cedb;
      padding: 18px;
      text-align: center;
    }
    .info {
      display: grid;
      gap: 4px;
      padding: 12px;
    }
    .title-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .device-id {
      font-weight: 650;
      word-break: break-word;
    }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      white-space: nowrap;
      font-size: 12px;
    }
    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--bad);
    }
    .online .dot { background: var(--good); }
    .links {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 4px;
    }
    .empty-page {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      color: var(--muted);
      padding: 22px;
    }
    @media (max-width: 760px) {
      header {
        align-items: flex-start;
        flex-direction: column;
      }
      .toolbar {
        justify-content: flex-start;
      }
      main {
        padding: 14px;
      }
      .grid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Camera Grid</h1>
      <div class="meta" id="serverMeta">Loading camera feeds</div>
    </div>
    <div class="toolbar">
      <label>
        Feed
        <select id="feedMode">
          <option value="video">Video</option>
          <option value="stream">Combined stream</option>
          <option value="capture">Capture</option>
        </select>
      </label>
      <span id="refreshState">Waiting for first refresh</span>
      <button id="refreshButton" type="button">Refresh</button>
      <a href="/dashboard">Dashboard</a>
    </div>
  </header>
  <main>
    <section class="grid" id="cameraGrid"></section>
  </main>
  <script>
    const state = {
      refreshMs: 5000,
      captureRefreshMs: 1500,
      captureTimer: null,
      lastDevices: [],
    };

    function text(value) {
      if (value === null || value === undefined || value === "") return "-";
      return String(value);
    }

    function el(tag, className, content) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (content !== undefined) node.textContent = content;
      return node;
    }

    function preferredEndpoint(device, mode) {
      const proxy = device.proxy_endpoints || {};
      if (mode === "capture") return proxy.capture || null;
      if (mode === "video") return proxy.video || proxy.stream || proxy.capture || null;
      return proxy.stream || proxy.video || proxy.capture || null;
    }

    function cameraDevices(devices) {
      return (devices || []).filter((device) => {
        const proxy = device.proxy_endpoints || {};
        return proxy.capture || proxy.video || proxy.stream;
      });
    }

    function link(name, url) {
      const item = el("a", "", name);
      item.href = url;
      item.target = "_blank";
      item.rel = "noreferrer";
      return item;
    }

    function render(data) {
      state.lastDevices = cameraDevices(data.devices);
      document.getElementById("serverMeta").textContent =
        `${state.lastDevices.length} camera-capable device(s) | server ${data.server.host}:${data.server.port}`;
      renderGrid();
      document.getElementById("refreshState").textContent = `Updated ${new Date().toLocaleTimeString()}`;
    }

    function renderGrid() {
      const mode = document.getElementById("feedMode").value;
      const root = document.getElementById("cameraGrid");
      root.replaceChildren();
      clearInterval(state.captureTimer);
      state.captureTimer = null;

      if (!state.lastDevices.length) {
        root.append(el("div", "empty-page", "No registered devices currently expose camera endpoints."));
        return;
      }

      for (const device of state.lastDevices) {
        const url = preferredEndpoint(device, mode);
        const card = el("article", "camera");
        const frame = el("div", "frame");
        if (url) {
          const img = document.createElement("img");
          img.alt = `${device.id} ${mode}`;
          img.dataset.baseSrc = url;
          img.src = mode === "capture" ? `${url}?t=${Date.now()}` : url;
          frame.append(img);
        } else {
          frame.append(el("div", "empty", `No ${mode} endpoint exposed.`));
        }

        const info = el("div", "info");
        const title = el("div", "title-row");
        title.append(el("div", "device-id", device.id));
        const status = el("span", `status ${device.online ? "online" : ""}`);
        status.append(el("span", "dot"));
        status.append(el("span", "", device.online ? "Online" : "Offline"));
        title.append(status);
        info.append(title);
        info.append(el("div", "meta", `${text(device.display_name)} | ${text(device.ip)} | ${text(device.online_detail)}`));
        const links = el("div", "links");
        const proxy = device.proxy_endpoints || {};
        for (const name of ["capture", "video", "stream"]) {
          if (proxy[name]) links.append(link(name, proxy[name]));
        }
        info.append(links);
        card.append(frame, info);
        root.append(card);
      }

      if (mode === "capture") {
        state.captureTimer = setInterval(() => {
          for (const img of document.querySelectorAll("img[data-base-src]")) {
            img.src = `${img.dataset.baseSrc}?t=${Date.now()}`;
          }
        }, state.captureRefreshMs);
      }
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
    document.getElementById("feedMode").addEventListener("change", renderGrid);
    refresh();
    setInterval(refresh, state.refreshMs);
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
        if parsed.path in ("/cameras", "/camera-grid"):
            html_response(self, 200, CAMERAS_HTML)
            return
        if parsed.path == "/dashboard-data":
            with STATE_LOCK:
                payload = dashboard_snapshot()
            json_response(self, 200, payload)
            return
        if parsed.path == "/health":
            json_response(self, 200, {"ok": True, "service": "spoken-command-server"})
            return
        if parsed.path.startswith("/media/"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) != 3:
                json_response(self, 404, {"error": "expected /media/{device_id}/{endpoint}"})
                return
            _media, device_text, endpoint_text = parts
            proxy_media(self, clean_device_id(device_text), clean_device_id(endpoint_text))
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

        if parsed.path.startswith("/devices/") and parsed.path.endswith("/friendly-name"):
            device_id = clean_device_id(parsed.path.removeprefix("/devices/").removesuffix("/friendly-name"))
            try:
                body = read_request_body(self)
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("friendly-name body must be a JSON object")
                with STATE_LOCK:
                    device = set_device_friendly_name(device_id, str(payload.get("friendly_name", "")))
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
            transcript_text = str(transcript.get("text", ""))
            device_response = command_response(transcript_text, device_id)
            record = {
                "device_id": device_id,
                "received_at": int(time.time()),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "ok": bool(device_response.get("ok")),
                "text": transcript_text,
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

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/devices/"):
            device_id = clean_device_id(parsed.path.removeprefix("/devices/"))
            with STATE_LOCK:
                existed = device_id in DEVICES or device_id in DEVICE_FRIENDLY_NAMES
                remove_device(device_id)
            json_response(self, 200, {"ok": True, "device_id": device_id, "removed": existed})
            return
        json_response(self, 404, {"error": "not found"})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> None:
    load_device_friendly_names()
    load_device_registry()
    server = ThreadingHTTPServer((HOST, PORT), CommandHandler)
    print(f"Listening on http://{HOST}:{PORT}")
    print("POST audio to /audio/command with ELEVENLABS_API_KEY set in the environment.")
    server.serve_forever()


if __name__ == "__main__":
    main()
