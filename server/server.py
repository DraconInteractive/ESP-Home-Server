#!/usr/bin/env python3
"""Local bridge server for ESP32 spoken-command audio.

The ESP32 posts either WAV bytes or raw signed 16-bit little-endian PCM. The
server wraps PCM as WAV, forwards it to ElevenLabs Speech to Text, interprets
the transcript as a local command, and returns a compact device response.
"""

from __future__ import annotations

import io
import json
import hashlib
import os
import glob
import platform
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, unquote, urlparse


HOST = os.environ.get("COMMAND_SERVER_HOST", "0.0.0.0")
PORT = int(os.environ.get("COMMAND_SERVER_PORT", "8080"))
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/speech-to-text"
MODEL_ID = os.environ.get("ELEVENLABS_MODEL_ID", "scribe_v2")
MAX_AUDIO_BYTES = int(os.environ.get("COMMAND_SERVER_MAX_AUDIO_BYTES", str(4 * 1024 * 1024)))
MAX_FIRMWARE_BYTES = int(os.environ.get("COMMAND_SERVER_MAX_FIRMWARE_BYTES", str(8 * 1024 * 1024)))
DEVICE_STALE_SECONDS = int(os.environ.get("COMMAND_SERVER_DEVICE_STALE_SECONDS", "45"))
DEVICE_PING_TIMEOUT_SECONDS = float(os.environ.get("COMMAND_SERVER_DEVICE_PING_TIMEOUT_SECONDS", "2.0"))
LOW_BATTERY_THRESHOLD_PERCENT = float(os.environ.get("COMMAND_SERVER_LOW_BATTERY_THRESHOLD_PERCENT", "20"))
MEDIA_SNAPSHOT_TTL_SECONDS = float(os.environ.get("COMMAND_SERVER_MEDIA_SNAPSHOT_TTL_SECONDS", "1.0"))
MEDIA_STREAM_IDLE_SECONDS = float(os.environ.get("COMMAND_SERVER_MEDIA_STREAM_IDLE_SECONDS", "3.0"))
MEDIA_STREAM_CHUNK_SIZE = int(os.environ.get("COMMAND_SERVER_MEDIA_STREAM_CHUNK_SIZE", "4096"))
RELAY_ENABLED = os.environ.get("COMMAND_SERVER_RELAY_ENABLED", "0") == "1"
RELAY_URL = os.environ.get("COMMAND_SERVER_RELAY_URL", "").rstrip("/")
RELAY_SYNC_TOKEN = os.environ.get("COMMAND_SERVER_RELAY_SYNC_TOKEN", "")
RELAY_POLL_SECONDS = max(2.0, float(os.environ.get("COMMAND_SERVER_RELAY_POLL_SECONDS", "5.0")))
RELAY_SNAPSHOT_SECONDS = max(10.0, float(os.environ.get("COMMAND_SERVER_RELAY_SNAPSHOT_SECONDS", "30.0")))
RELAY_TIMEOUT_SECONDS = max(2.0, float(os.environ.get("COMMAND_SERVER_RELAY_TIMEOUT_SECONDS", "10.0")))
RELAY_PAIRING_ENABLED = os.environ.get("COMMAND_SERVER_RELAY_PAIRING_ENABLED", "1") == "1"
RELAY_PAIRING_URL = os.environ.get("COMMAND_SERVER_RELAY_PAIRING_URL", RELAY_URL).rstrip("/")
RELAY_IP_PAIRING_TOKEN = os.environ.get("COMMAND_SERVER_RELAY_IP_PAIRING_TOKEN", "")
RELAY_PAIRING_DEVICE_ID = os.environ.get("COMMAND_SERVER_RELAY_PAIRING_DEVICE_ID", "home-server")
RELAY_PAIRING_NAME = os.environ.get("COMMAND_SERVER_RELAY_PAIRING_NAME", "Home Server")
RELAY_PAIRING_TYPE = os.environ.get("COMMAND_SERVER_RELAY_PAIRING_TYPE", "antix-server")
RELAY_PAIRING_PORTS = os.environ.get("COMMAND_SERVER_RELAY_PAIRING_PORTS", "ssh:22,dashboard:8080")
RELAY_PAIRING_NOTES = os.environ.get("COMMAND_SERVER_RELAY_PAIRING_NOTES", "Primary local command server")
RELAY_PAIRING_LOCAL_IPS = os.environ.get("COMMAND_SERVER_RELAY_PAIRING_LOCAL_IPS", "")
RELAY_PAIRING_SECONDS = max(60.0, float(os.environ.get("COMMAND_SERVER_RELAY_PAIRING_SECONDS", "300.0")))
DEVICE_NAMES_PATH = os.environ.get(
    "COMMAND_SERVER_DEVICE_NAMES_PATH",
    os.path.join(os.path.dirname(__file__), "device-names.json"),
)
DEVICE_REGISTRY_PATH = os.environ.get(
    "COMMAND_SERVER_DEVICE_REGISTRY_PATH",
    os.path.join(os.path.dirname(__file__), "device-registry.json"),
)
RULES_PATH = os.environ.get(
    "COMMAND_SERVER_RULES_PATH",
    os.path.join(os.path.dirname(__file__), "rules.json"),
)
TIMER_STATE_PATH = os.environ.get(
    "COMMAND_SERVER_TIMER_STATE_PATH",
    os.path.join(os.path.dirname(__file__), "timers.json"),
)
UPTIME_MONITORS_PATH = os.environ.get(
    "COMMAND_SERVER_UPTIME_MONITORS_PATH",
    os.path.join(os.path.dirname(__file__), "uptime-monitors.json"),
)
MISSION_BOARD_PATH = os.environ.get(
    "COMMAND_SERVER_MISSION_BOARD_PATH",
    os.path.join(os.path.dirname(__file__), "mission-board.json"),
)
R1_NOTE_PATH = os.environ.get(
    "COMMAND_SERVER_R1_NOTE_PATH",
    os.path.join(os.path.dirname(__file__), "r1-note.json"),
)
DATABASE_PATH = os.environ.get(
    "COMMAND_SERVER_DATABASE_PATH",
    os.path.join(os.path.dirname(__file__), "server-state.sqlite3"),
)
FIRMWARE_CATALOG_PATH = os.environ.get(
    "COMMAND_SERVER_FIRMWARE_CATALOG_PATH",
    os.path.join(os.path.dirname(__file__), "firmware-catalog.json"),
)
FIRMWARE_BLOB_DIR = os.environ.get(
    "COMMAND_SERVER_FIRMWARE_BLOB_DIR",
    os.path.join(os.path.dirname(__file__), "firmware-catalog"),
)
R1_UPDATE_MANIFEST_PATH = os.environ.get(
    "COMMAND_SERVER_R1_UPDATE_MANIFEST_PATH",
    os.path.join(os.path.dirname(__file__), "r1-update.json"),
)
R1_APK_DIR = os.environ.get(
    "COMMAND_SERVER_R1_APK_DIR",
    os.path.join(os.path.dirname(__file__), "r1-apk"),
)
ACTION_CONFIG_PATH = os.environ.get(
    "COMMAND_SERVER_ACTION_CONFIG_PATH",
    os.path.join(os.path.dirname(__file__), "actions", "actions.json"),
)
ACTION_SCRIPT_DIR = os.environ.get(
    "COMMAND_SERVER_ACTION_SCRIPT_DIR",
    os.path.join(os.path.dirname(__file__), "actions"),
)
ACTION_TIMEOUT_SECONDS = float(os.environ.get("COMMAND_SERVER_ACTION_TIMEOUT_SECONDS", "15.0"))
RECENT_HISTORY_LIMIT = int(os.environ.get("COMMAND_SERVER_RECENT_HISTORY_LIMIT", "100"))
RESTART_ENABLED = os.environ.get("COMMAND_SERVER_RESTART_ENABLED", "0") == "1"
RESTART_COMMAND = shlex.split(
    os.environ.get("COMMAND_SERVER_RESTART_COMMAND", "sudo -n sv restart spoken-command-server")
)
RESTART_DELAY_SECONDS = max(0.1, float(os.environ.get("COMMAND_SERVER_RESTART_DELAY_SECONDS", "0.5")))
SERVER_STARTED_AT = int(time.time())

RECENT_COMMANDS: list[dict[str, Any]] = []
RECENT_BUTTON_EVENTS: list[dict[str, Any]] = []
RECENT_RULE_RUNS: list[dict[str, Any]] = []
ACTIVE_TIMERS: dict[str, dict[str, Any]] = {}
UPTIME_MONITORS: dict[str, dict[str, Any]] = {}
MISSION_TASKS: dict[str, dict[str, Any]] = {}
R1_NOTE: dict[str, Any] = {"label": "r1-note", "text": "", "updated_at": 0}
LOW_BATTERY_NOTIFIED: set[str] = set()
MUTED_DEVICES: dict[str, bool] = {}
GLOBAL_MUTED = False
PENDING_ACTIONS: dict[str, dict[str, Any]] = {}
DEVICES: dict[str, dict[str, Any]] = {}
DEVICE_EVENTS: dict[str, list[dict[str, Any]]] = {}
DEVICE_FRIENDLY_NAMES: dict[str, str] = {}
EVENT_RULES: dict[str, dict[str, Any]] = {}
FIRMWARE_CATALOG: dict[str, dict[str, Any]] = {}
SCRIPT_ACTIONS: dict[str, dict[str, Any]] = {}
SCRIPT_ACTION_ALIASES: dict[str, str] = {}
MEDIA_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
MEDIA_STREAMS: dict[tuple[str, str], "MediaStreamProxy"] = {}
LAST_CPU_SAMPLE: dict[str, int] | None = None
LAST_SYSTEM_INFO: dict[str, Any] | None = None
LAST_SYSTEM_INFO_AT = 0.0
STATE_LOCK = threading.RLock()
TIMER_CONDITION = threading.Condition(STATE_LOCK)

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


def read_request_body(handler: BaseHTTPRequestHandler, max_bytes: int = MAX_AUDIO_BYTES) -> bytes:
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
            if total > max_bytes:
                raise ValueError(f"request body too large: {total} bytes")
            chunks.append(handler.rfile.read(chunk_size))
            if handler.rfile.read(2) != b"\r\n":
                raise ValueError("invalid chunk terminator")

        return b"".join(chunks)

    content_length = int(handler.headers.get("Content-Length", "0"))
    if content_length <= 0:
        raise ValueError("missing request body")
    if content_length > max_bytes:
        raise ValueError(f"request body too large: {content_length} bytes")
    return handler.rfile.read(content_length)


def read_optional_json_body(handler: BaseHTTPRequestHandler, max_bytes: int = MAX_AUDIO_BYTES) -> dict[str, Any]:
    content_length = int(handler.headers.get("Content-Length", "0"))
    if content_length <= 0 and handler.headers.get("Transfer-Encoding", "").lower() != "chunked":
        return {}
    body = read_request_body(handler, max_bytes)
    if not body:
        return {}
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


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


def clean_device_type(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", str(value).strip().lower())
    return cleaned[:64] or "unknown"


def clean_version(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.:+-]+", "-", str(value).strip())
    return cleaned[:80] or str(int(time.time()))


def clean_filename(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", os.path.basename(str(value).strip()))
    return cleaned[:120] or "firmware.bin"


def clean_sha256(value: str) -> str:
    cleaned = str(value).strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", cleaned):
        return cleaned
    return ""


def clean_action_name(value: str) -> str:
    cleaned = normalize_command_text(str(value))
    cleaned = re.sub(r"[^a-z0-9 _.-]+", "", cleaned)
    return " ".join(cleaned.split())[:80]


def action_public_metadata(name: str, action: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "aliases": action.get("aliases", []),
        "description": action.get("description", ""),
        "timeout_seconds": action.get("timeout_seconds", ACTION_TIMEOUT_SECONDS),
        "requires_confirmation": bool(action.get("requires_confirmation", False)),
    }


def path_status(path: str) -> dict[str, Any]:
    directory = path if os.path.isdir(path) else os.path.dirname(path)
    return {
        "path": path,
        "exists": os.path.exists(path),
        "directory": directory,
        "directory_exists": os.path.isdir(directory),
        "directory_writable": os.access(directory or ".", os.W_OK),
    }


def startup_diagnostics() -> list[dict[str, Any]]:
    checks = [
        {
            "name": "ElevenLabs API key",
            "ok": bool(os.environ.get("ELEVENLABS_API_KEY")),
            "detail": "configured" if os.environ.get("ELEVENLABS_API_KEY") else "ELEVENLABS_API_KEY is not set",
        },
        {
            "name": "Device registry",
            "ok": path_status(DEVICE_REGISTRY_PATH)["directory_writable"],
            "detail": DEVICE_REGISTRY_PATH,
        },
        {
            "name": "SQLite state",
            "ok": path_status(DATABASE_PATH)["directory_writable"],
            "detail": DATABASE_PATH,
        },
        {
            "name": "Actions",
            "ok": bool(SCRIPT_ACTIONS),
            "detail": f"{len(SCRIPT_ACTIONS)} loaded from {ACTION_CONFIG_PATH}",
        },
        {
            "name": "ntfy",
            "ok": bool(os.environ.get("COMMAND_SERVER_NTFY_TOPIC")),
            "detail": "configured" if os.environ.get("COMMAND_SERVER_NTFY_TOPIC") else "COMMAND_SERVER_NTFY_TOPIC is not set",
        },
        {
            "name": "SMTP email",
            "ok": bool(os.environ.get("COMMAND_SERVER_SMTP_HOST")),
            "detail": "configured" if os.environ.get("COMMAND_SERVER_SMTP_HOST") else "COMMAND_SERVER_SMTP_HOST is not set",
        },
    ]
    return checks


def schedule_server_restart() -> None:
    if not RESTART_ENABLED:
        raise RuntimeError("server restart is not enabled")
    if not RESTART_COMMAND:
        raise RuntimeError("server restart command is empty")

    def runner() -> None:
        time.sleep(RESTART_DELAY_SECONDS)
        try:
            subprocess.Popen(
                RESTART_COMMAND,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            print(f"Server restart command failed to start: {exc}")

    threading.Thread(target=runner, name="server-restart", daemon=True).start()


def load_script_actions() -> None:
    SCRIPT_ACTIONS.clear()
    SCRIPT_ACTION_ALIASES.clear()
    try:
        with open(ACTION_CONFIG_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return
    except Exception as exc:
        print(f"Could not load server actions: {exc}")
        return

    entries = payload.get("actions") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return

    base_dir = os.path.realpath(ACTION_SCRIPT_DIR)
    for raw_action in entries:
        if not isinstance(raw_action, dict):
            continue
        name = clean_action_name(str(raw_action.get("name", "")))
        script_name = clean_filename(str(raw_action.get("script", "")))
        if not name or not script_name:
            continue
        script_path = os.path.realpath(os.path.join(base_dir, script_name))
        if os.path.commonpath([base_dir, script_path]) != base_dir:
            print(f"Skipping action {name}: script path is outside action directory")
            continue
        if not os.path.isfile(script_path):
            print(f"Skipping action {name}: script not found: {script_name}")
            continue
        if not os.access(script_path, os.X_OK):
            print(f"Skipping action {name}: script is not executable: {script_name}")
            continue

        aliases = []
        for alias in raw_action.get("aliases", []):
            cleaned_alias = clean_action_name(str(alias))
            if cleaned_alias and cleaned_alias not in aliases:
                aliases.append(cleaned_alias)
        if name not in aliases:
            aliases.insert(0, name)

        args = raw_action.get("args", [])
        if not isinstance(args, list):
            args = []
        safe_args = [str(arg)[:200] for arg in args if isinstance(arg, (str, int, float))]
        try:
            timeout_seconds = float(raw_action.get("timeout_seconds", ACTION_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            timeout_seconds = ACTION_TIMEOUT_SECONDS
        timeout_seconds = max(1.0, min(timeout_seconds, 120.0))

        action = {
            "name": name,
            "aliases": aliases,
            "description": str(raw_action.get("description", ""))[:240],
            "script_path": script_path,
            "script": script_name,
            "args": safe_args,
            "timeout_seconds": timeout_seconds,
            "requires_confirmation": bool(raw_action.get("requires_confirmation", False)),
        }
        SCRIPT_ACTIONS[name] = action
        for alias in aliases:
            SCRIPT_ACTION_ALIASES[alias] = name


def list_script_actions() -> list[dict[str, Any]]:
    return [
        action_public_metadata(name, SCRIPT_ACTIONS[name])
        for name in sorted(SCRIPT_ACTIONS)
    ]


def resolve_script_action(reference: str) -> str | None:
    normalized = clean_action_name(reference)
    if not normalized:
        return None
    if normalized in SCRIPT_ACTIONS:
        return normalized
    if normalized in SCRIPT_ACTION_ALIASES:
        return SCRIPT_ACTION_ALIASES[normalized]

    matches: list[tuple[int, str]] = []
    for alias, name in SCRIPT_ACTION_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", normalized):
            matches.append((len(alias), name))
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


def run_script_action(action_name: str, device_id: str, transcript: str) -> dict[str, Any]:
    action = SCRIPT_ACTIONS.get(action_name)
    if not action:
        raise ValueError(f"unknown action: {action_name}")

    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "SCD_ACTION_NAME": action_name,
        "SCD_DEVICE_ID": device_id,
        "SCD_TRANSCRIPT": transcript,
    }
    for key, value in os.environ.items():
        if key.startswith(("COMMAND_SERVER_SMTP_", "COMMAND_SERVER_EMAIL_", "COMMAND_SERVER_NTFY_")):
            env[key] = value
    started = time.monotonic()
    result = subprocess.run(
        [action["script_path"], *action.get("args", [])],
        cwd=ACTION_SCRIPT_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=float(action.get("timeout_seconds", ACTION_TIMEOUT_SECONDS)),
        check=False,
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    return {
        "name": action_name,
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": stdout[:2000],
        "stderr": stderr[:2000],
        "duration_ms": duration_ms,
    }


def infer_device_type(device_id: str, device: dict[str, Any]) -> str:
    explicit = device.get("device_type") or device.get("firmware_device_type")
    if explicit:
        return clean_device_type(str(explicit))
    if device_id.startswith("waveshare-c6-"):
        return "waveshare-c6-voice-controller"
    if device_id.startswith("waveshare-c3-display-"):
        return "waveshare-c3-round-display"
    if device_id.startswith("timercam-x-"):
        return "timercam-x"
    if device_id.startswith("esp-eye-"):
        return "esp-eye"
    if device_id.startswith("xiao-button-"):
        return "xiao-button"
    if device_id.startswith("arduino-nesso-n1-"):
        return "arduino-nesso-n1"
    if device_id.startswith("waveshare-c6-lcd147-"):
        return "waveshare-c6-lcd-147"
    return clean_device_type(str(device.get("type") or "unknown"))


def load_firmware_catalog() -> None:
    try:
        with open(FIRMWARE_CATALOG_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return
    except Exception as exc:
        print(f"Could not load firmware catalog: {exc}")
        return

    entries = payload.get("firmware") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        return
    for raw_device_type, raw_entry in entries.items():
        if isinstance(raw_entry, dict):
            device_type = clean_device_type(str(raw_device_type))
            FIRMWARE_CATALOG[device_type] = raw_entry


def save_firmware_catalog() -> None:
    directory = os.path.dirname(FIRMWARE_CATALOG_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = {"firmware": {key: FIRMWARE_CATALOG[key] for key in sorted(FIRMWARE_CATALOG)}}
    temp_path = f"{FIRMWARE_CATALOG_PATH}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    os.replace(temp_path, FIRMWARE_CATALOG_PATH)


def firmware_catalog_summary() -> list[dict[str, Any]]:
    result = []
    for device_type, entry in sorted(FIRMWARE_CATALOG.items()):
        versions = entry.get("versions", {})
        if not isinstance(versions, dict):
            versions = {}
        latest_version = str(entry.get("latest_version") or "")
        latest = versions.get(latest_version, {}) if latest_version else {}
        result.append({
            "device_type": device_type,
            "latest_version": latest_version,
            "version_count": len(versions),
            "latest": latest if isinstance(latest, dict) else {},
        })
    return result


def firmware_binary_path(device_type: str, version: str, filename: str) -> str:
    return os.path.join(FIRMWARE_BLOB_DIR, clean_device_type(device_type), clean_version(version), clean_filename(filename))


def add_firmware_catalog_entry(device_type: str, version: str, metadata: dict[str, Any]) -> dict[str, Any]:
    cleaned_type = clean_device_type(device_type)
    cleaned_version = clean_version(version)
    entry = FIRMWARE_CATALOG.setdefault(cleaned_type, {"device_type": cleaned_type, "versions": {}})
    versions = entry.setdefault("versions", {})
    if not isinstance(versions, dict):
        versions = {}
        entry["versions"] = versions

    record = dict(metadata)
    record["device_type"] = cleaned_type
    record["version"] = cleaned_version
    record["created_at"] = int(record.get("created_at") or time.time())
    versions[cleaned_version] = record
    entry["latest_version"] = cleaned_version
    save_firmware_catalog()
    return record


def remove_firmware_catalog_entry(device_type: str, version: str | None = None) -> bool:
    cleaned_type = clean_device_type(device_type)
    entry = FIRMWARE_CATALOG.get(cleaned_type)
    if not entry:
        target = os.path.join(FIRMWARE_BLOB_DIR, cleaned_type)
        had_files = os.path.exists(target)
        if not version:
            shutil.rmtree(target, ignore_errors=True)
        elif version:
            version_target = os.path.join(target, clean_version(version))
            had_files = os.path.exists(version_target)
            shutil.rmtree(version_target, ignore_errors=True)
        save_firmware_catalog()
        return had_files
    if version:
        cleaned_version = clean_version(version)
        versions = entry.get("versions", {})
        if isinstance(versions, dict):
            removed = versions.pop(cleaned_version, None) is not None
            if entry.get("latest_version") == cleaned_version:
                entry["latest_version"] = sorted(versions.keys())[-1] if versions else ""
            if not versions:
                FIRMWARE_CATALOG.pop(cleaned_type, None)
            shutil.rmtree(os.path.join(FIRMWARE_BLOB_DIR, cleaned_type, cleaned_version), ignore_errors=True)
            save_firmware_catalog()
            return removed
        return False
    FIRMWARE_CATALOG.pop(cleaned_type, None)
    shutil.rmtree(os.path.join(FIRMWARE_BLOB_DIR, cleaned_type), ignore_errors=True)
    save_firmware_catalog()
    return True


def r1_update_manifest() -> dict[str, Any]:
    try:
        with open(R1_UPDATE_MANIFEST_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {"ok": False}
    except Exception as exc:
        print(f"Could not load r1 update manifest: {exc}")
        return {"ok": False}
    if not isinstance(payload, dict) or payload.get("ok") is False:
        return {"ok": False}

    try:
        version_code = int(payload.get("version_code"))
    except (TypeError, ValueError):
        return {"ok": False}
    version_name = str(payload.get("version_name", "")).strip()[:80]
    url = str(payload.get("url", "")).strip()[:240]
    if version_code < 0 or not version_name or not url:
        return {"ok": False}

    manifest: dict[str, Any] = {
        "ok": True,
        "version_code": version_code,
        "version_name": version_name,
        "url": url,
    }
    try:
        if payload.get("size_bytes") is not None:
            manifest["size_bytes"] = max(0, int(payload.get("size_bytes")))
    except (TypeError, ValueError):
        pass
    sha256 = clean_sha256(str(payload.get("sha256", "")))
    if sha256:
        manifest["sha256"] = sha256
    notes = str(payload.get("notes", "")).strip()
    if notes:
        manifest["notes"] = notes[:2000]
    return manifest


def r1_apk_path(filename: str) -> tuple[str, str]:
    cleaned_filename = clean_filename(unquote(filename))
    if not cleaned_filename.lower().endswith(".apk"):
        raise ValueError("APK filename must end with .apk")
    path = os.path.realpath(os.path.join(R1_APK_DIR, cleaned_filename))
    base = os.path.realpath(R1_APK_DIR)
    if os.path.commonpath([base, path]) != base:
        raise ValueError("APK path is outside configured directory")
    return cleaned_filename, path


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
            CREATE TABLE IF NOT EXISTS command_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at INTEGER NOT NULL,
                device_id TEXT NOT NULL,
                duration_ms INTEGER,
                ok INTEGER NOT NULL,
                text TEXT,
                display_text TEXT,
                tone TEXT,
                command TEXT,
                muted INTEGER NOT NULL DEFAULT 0,
                state_json TEXT NOT NULL DEFAULT '{}',
                transcript_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_command_history_device_time
            ON command_history(device_id, received_at)
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS button_event_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at INTEGER NOT NULL,
                device_id TEXT NOT NULL,
                event TEXT,
                button TEXT,
                gpio TEXT,
                active_low INTEGER,
                click_count INTEGER,
                uptime_ms INTEGER,
                remote_addr TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}'
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_button_event_history_device_time
            ON button_event_history(device_id, received_at)
        """)


def load_recent_history() -> None:
    with db_connect() as connection:
        command_rows = connection.execute(
            """
            SELECT * FROM command_history
            ORDER BY id DESC
            LIMIT ?
            """,
            (RECENT_HISTORY_LIMIT,),
        ).fetchall()
        button_rows = connection.execute(
            """
            SELECT * FROM button_event_history
            ORDER BY id DESC
            LIMIT ?
            """,
            (RECENT_HISTORY_LIMIT,),
        ).fetchall()

    for row in reversed(command_rows):
        try:
            state = json.loads(row["state_json"] or "{}")
        except json.JSONDecodeError:
            state = {}
        try:
            transcript = json.loads(row["transcript_json"] or "{}")
        except json.JSONDecodeError:
            transcript = {}
        RECENT_COMMANDS.append({
            "device_id": row["device_id"],
            "received_at": row["received_at"],
            "duration_ms": row["duration_ms"],
            "ok": bool(row["ok"]),
            "text": row["text"] or "",
            "display_text": row["display_text"] or "",
            "tone": row["tone"] or "",
            "command": row["command"],
            "state": state,
            "muted": bool(row["muted"]),
            "transcript": transcript,
        })

    for row in reversed(button_rows):
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        event = {
            "device_id": row["device_id"],
            "received_at": row["received_at"],
            "event": row["event"] or "",
            "button": row["button"] or "",
            "gpio": payload.get("gpio"),
            "active_low": bool(row["active_low"]) if row["active_low"] is not None else payload.get("active_low"),
            "click_count": row["click_count"],
            "uptime_ms": row["uptime_ms"],
            "remote_addr": row["remote_addr"],
        }
        RECENT_BUTTON_EVENTS.append(event)


def save_command_record(record: dict[str, Any]) -> None:
    with db_connect() as connection:
        connection.execute(
            """
            INSERT INTO command_history (
                received_at, device_id, duration_ms, ok, text, display_text,
                tone, command, muted, state_json, transcript_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(record.get("received_at") or time.time()),
                clean_device_id(str(record.get("device_id", "unknown"))),
                record.get("duration_ms"),
                1 if record.get("ok") else 0,
                str(record.get("text", "")),
                str(record.get("display_text", "")),
                str(record.get("tone", "")),
                record.get("command"),
                1 if record.get("muted") else 0,
                json.dumps(record.get("state", {}), separators=(",", ":")),
                json.dumps(record.get("transcript", {}), separators=(",", ":")),
            ),
        )


def record_command_result(record: dict[str, Any]) -> None:
    update_device_result(clean_device_id(str(record.get("device_id", "unknown"))), {
        "command": record.get("command"),
        "transcript": record.get("text", ""),
        "display_text": record.get("display_text", ""),
    })
    RECENT_COMMANDS.append(record)
    del RECENT_COMMANDS[:-RECENT_HISTORY_LIMIT]
    save_command_record(record)
    transcript = record.get("transcript", {})
    source = transcript.get("source") if isinstance(transcript, dict) else ""
    if source != "rule":
        dispatch_server_event(
            "command",
            clean_device_id(str(record.get("device_id", "unknown"))),
            command=str(record.get("command") or ""),
            transcript=str(record.get("text") or ""),
            ok=bool(record.get("ok")),
        )


def save_button_event_record(event: dict[str, Any]) -> None:
    with db_connect() as connection:
        connection.execute(
            """
            INSERT INTO button_event_history (
                received_at, device_id, event, button, gpio, active_low,
                click_count, uptime_ms, remote_addr, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(event.get("received_at") or time.time()),
                clean_device_id(str(event.get("device_id", "unknown"))),
                str(event.get("event", "")),
                str(event.get("button", "")),
                None if event.get("gpio") is None else str(event.get("gpio")),
                None if event.get("active_low") is None else (1 if event.get("active_low") else 0),
                event.get("click_count"),
                event.get("uptime_ms"),
                event.get("remote_addr"),
                json.dumps(event, separators=(",", ":")),
            ),
        )


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
        "device_type",
        "firmware",
        "firmware_version",
        "firmware_project",
        "firmware_build",
        "last_seen_via",
        "last_local_seen",
        "last_relay_seen",
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


def clean_rule_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", str(value).strip())
    return cleaned[:80] or uuid.uuid4().hex


def clean_rule_type(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", str(value).strip().lower())
    return cleaned[:40] or "button"


def clean_mission_task_type(value: str) -> str:
    cleaned = str(value).strip().lower()
    if cleaned in {"daily", "today"}:
        return "daily"
    return "persistent"


def clean_mission_title(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value).strip())
    if not cleaned:
        raise ValueError("title is required")
    return cleaned[:160]


def clean_note_text(value: str) -> str:
    return str(value).replace("\r\n", "\n").replace("\r", "\n")[:10000]


def public_r1_note() -> dict[str, Any]:
    return {
        "label": "r1-note",
        "text": str(R1_NOTE.get("text", "")),
        "updated_at": int(R1_NOTE.get("updated_at", 0) or 0),
    }


def save_r1_note() -> None:
    directory = os.path.dirname(R1_NOTE_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temp_path = f"{R1_NOTE_PATH}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(public_r1_note(), handle, indent=2)
        handle.write("\n")
    os.replace(temp_path, R1_NOTE_PATH)


def load_r1_note() -> None:
    try:
        with open(R1_NOTE_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return
    except Exception as exc:
        print(f"Could not load r1-note: {exc}")
        return
    if not isinstance(payload, dict):
        return
    R1_NOTE["label"] = "r1-note"
    R1_NOTE["text"] = clean_note_text(str(payload.get("text", "")))
    R1_NOTE["updated_at"] = int(payload.get("updated_at", 0) or 0)


def set_r1_note(payload: dict[str, Any], updated_by: str = "local") -> dict[str, Any]:
    now = int(time.time())
    R1_NOTE["label"] = "r1-note"
    R1_NOTE["text"] = clean_note_text(str(payload.get("text", payload.get("note", ""))))
    R1_NOTE["updated_at"] = int(payload.get("updated_at", now) or now)
    R1_NOTE["updated_by"] = str(updated_by)[:80]
    save_r1_note()
    return public_r1_note()


def local_date_text(timestamp: int | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(timestamp or int(time.time())))


def public_mission_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task.get("id", ""),
        "title": task.get("title", ""),
        "notes": task.get("notes", ""),
        "task_type": task.get("task_type", "persistent"),
        "status": task.get("status", "open"),
        "created_at": task.get("created_at", 0),
        "due_date": task.get("due_date"),
        "completed_at": task.get("completed_at"),
        "completed_by": task.get("completed_by", ""),
        "source": task.get("source", ""),
    }


def open_mission_tasks() -> list[dict[str, Any]]:
    today = local_date_text()
    tasks: list[dict[str, Any]] = []
    for task in MISSION_TASKS.values():
        if task.get("status") == "completed":
            continue
        if str(task.get("task_type", "persistent")) == "daily" and str(task.get("due_date", "")) < today:
            continue
        tasks.append(public_mission_task(task))
    return sorted(
        tasks,
        key=lambda item: (
            str(item.get("due_date") or "9999-99-99"),
            str(item.get("task_type") or ""),
            int(item.get("created_at") or 0),
            str(item.get("title") or ""),
        ),
    )


def active_mission_tasks(tasks: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    today = local_date_text()
    active: list[dict[str, Any]] = []
    for task in tasks if tasks is not None else open_mission_tasks():
        task_type = str(task.get("task_type", "persistent"))
        if task_type == "daily" and str(task.get("due_date", "")) != today:
            continue
        active.append(task)
    return sorted(active, key=lambda item: (str(item.get("task_type") or ""), int(item.get("created_at") or 0), str(item.get("title") or "")))


def mission_board_summary() -> dict[str, Any]:
    tasks = open_mission_tasks()
    active_tasks = active_mission_tasks(tasks)
    future_tasks = [
        task for task in tasks
        if task.get("task_type") == "daily" and str(task.get("due_date", "")) > local_date_text()
    ]
    return {
        "today": local_date_text(),
        "tasks": tasks,
        "active_tasks": active_tasks,
        "future_tasks": future_tasks,
        "total_open": len(tasks),
        "total_active": len(active_tasks),
        "persistent_count": sum(1 for task in tasks if task.get("task_type") == "persistent"),
        "daily_count": sum(1 for task in tasks if task.get("task_type") == "daily"),
        "future_count": len(future_tasks),
    }


def save_mission_tasks() -> None:
    directory = os.path.dirname(MISSION_BOARD_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = {"tasks": [MISSION_TASKS[task_id] for task_id in sorted(MISSION_TASKS)]}
    temp_path = f"{MISSION_BOARD_PATH}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    os.replace(temp_path, MISSION_BOARD_PATH)


def load_mission_tasks() -> None:
    try:
        with open(MISSION_BOARD_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return
    except Exception as exc:
        print(f"Could not load mission board: {exc}")
        return
    for raw in payload.get("tasks", []) if isinstance(payload, dict) else []:
        if not isinstance(raw, dict):
            continue
        try:
            task_id = clean_rule_id(str(raw.get("id", "")) or uuid.uuid4().hex)
            title = clean_mission_title(str(raw.get("title", "")))
        except Exception:
            continue
        task_type = clean_mission_task_type(str(raw.get("task_type", "persistent")))
        MISSION_TASKS[task_id] = {
            "id": task_id,
            "title": title,
            "notes": str(raw.get("notes", ""))[:1000],
            "task_type": task_type,
            "status": "completed" if raw.get("status") == "completed" else "open",
            "created_at": int(raw.get("created_at", int(time.time())) or int(time.time())),
            "due_date": str(raw.get("due_date", ""))[:10] if task_type == "daily" else None,
            "completed_at": raw.get("completed_at"),
            "completed_by": str(raw.get("completed_by", ""))[:80],
            "source": str(raw.get("source", ""))[:80],
        }


def create_mission_task(payload: dict[str, Any], source: str = "local") -> dict[str, Any]:
    now = int(time.time())
    task_type = clean_mission_task_type(str(payload.get("task_type", "persistent")))
    due_date = str(payload.get("due_date", "")).strip()[:10]
    if task_type == "daily" and not re.match(r"^\d{4}-\d{2}-\d{2}$", due_date):
        due_date = local_date_text(now)
    task_id = clean_rule_id(str(payload.get("id", "")) or uuid.uuid4().hex)
    task = {
        "id": task_id,
        "title": clean_mission_title(str(payload.get("title", ""))),
        "notes": str(payload.get("notes", ""))[:1000],
        "task_type": task_type,
        "status": "open",
        "created_at": now,
        "due_date": due_date if task_type == "daily" else None,
        "completed_at": None,
        "completed_by": "",
        "source": source[:80],
    }
    MISSION_TASKS[task_id] = task
    save_mission_tasks()
    return public_mission_task(task)


def complete_mission_task(task_id: str, completed_by: str = "") -> dict[str, Any]:
    cleaned_id = clean_rule_id(task_id)
    task = MISSION_TASKS.get(cleaned_id)
    if not task:
        raise ValueError("mission task not found")
    task["status"] = "completed"
    task["completed_at"] = int(time.time())
    task["completed_by"] = str(completed_by or "dashboard")[:80]
    save_mission_tasks()
    return public_mission_task(task)


def clean_rule_step(raw_step: dict[str, Any]) -> dict[str, Any]:
    action_type = str(raw_step.get("action_type", raw_step.get("type", "transcript")))[:40]
    return {
        "action_type": action_type,
        "transcript": str(raw_step.get("transcript", ""))[:500],
        "action_name": str(raw_step.get("action_name", ""))[:80],
        "action_transcript": str(raw_step.get("action_transcript", ""))[:500],
    }


def rule_steps(rule: dict[str, Any]) -> list[dict[str, Any]]:
    raw_steps = rule.get("steps")
    if isinstance(raw_steps, list) and raw_steps:
        return [
            clean_rule_step(step)
            for step in raw_steps
            if isinstance(step, dict)
        ][:8]
    return [clean_rule_step(rule)]


def public_rule(rule_id: str, rule: dict[str, Any]) -> dict[str, Any]:
    steps = rule_steps(rule)
    return {
        "id": rule_id,
        "enabled": bool(rule.get("enabled", True)),
        "name": str(rule.get("name", rule_id))[:80],
        "event_type": clean_rule_type(str(rule.get("event_type", "button"))),
        "device_id": str(rule.get("device_id", ""))[:80],
        "button": str(rule.get("button", ""))[:40],
        "capability": str(rule.get("capability", ""))[:40],
        "command": str(rule.get("command", ""))[:80],
        "steps": steps,
        "action_type": steps[0].get("action_type", "transcript") if steps else "transcript",
        "transcript": steps[0].get("transcript", "") if steps else "",
        "action_name": steps[0].get("action_name", "") if steps else "",
        "action_transcript": steps[0].get("action_transcript", "") if steps else "",
        "created_at": rule.get("created_at"),
        "last_run_at": rule.get("last_run_at"),
        "last_result": rule.get("last_result", ""),
    }


def load_event_rules() -> None:
    try:
        with open(RULES_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return
    except Exception as exc:
        print(f"Could not load event rules: {exc}")
        return

    entries = payload.get("rules") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return
    for raw_rule in entries:
        if not isinstance(raw_rule, dict):
            continue
        rule_id = clean_rule_id(str(raw_rule.get("id", "")))
        EVENT_RULES[rule_id] = public_rule(rule_id, raw_rule)


def save_event_rules() -> None:
    directory = os.path.dirname(RULES_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = {"rules": [public_rule(rule_id, EVENT_RULES[rule_id]) for rule_id in sorted(EVENT_RULES)]}
    temp_path = f"{RULES_PATH}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    os.replace(temp_path, RULES_PATH)


def upsert_event_rule(payload: dict[str, Any]) -> dict[str, Any]:
    rule_id = clean_rule_id(str(payload.get("id", "")) or uuid.uuid4().hex)
    rule = public_rule(rule_id, payload)
    rule["created_at"] = int(rule.get("created_at") or time.time())
    EVENT_RULES[rule_id] = rule
    save_event_rules()
    return public_rule(rule_id, rule)


def rule_matches_event(rule: dict[str, Any], event: dict[str, Any]) -> bool:
    if not rule.get("enabled", True):
        return False
    if clean_rule_type(str(rule.get("event_type", "button"))) != clean_rule_type(str(event.get("event_type", "button"))):
        return False
    rule_device = str(rule.get("device_id", "")).strip()
    if rule_device and clean_device_id(rule_device) != clean_device_id(str(event.get("device_id", ""))):
        return False
    rule_button = str(rule.get("button", "")).strip()
    if rule_button and rule_button != str(event.get("button", "")).strip():
        return False
    rule_capability = str(rule.get("capability", "")).strip()
    if rule_capability and rule_capability != str(event.get("capability", "")).strip():
        return False
    rule_command = normalize_command_text(str(rule.get("command", "")))
    if rule_command and rule_command != normalize_command_text(str(event.get("command", ""))):
        return False
    return True


def record_rule_run(rule_id: str, event: dict[str, Any], ok: bool, result: str) -> None:
    now = int(time.time())
    if rule_id in EVENT_RULES:
        EVENT_RULES[rule_id]["last_run_at"] = now
        EVENT_RULES[rule_id]["last_result"] = result[:200]
        save_event_rules()
    RECENT_RULE_RUNS.append({
        "rule_id": rule_id,
        "received_at": now,
        "device_id": event.get("device_id"),
        "event_type": event.get("event_type"),
        "ok": ok,
        "result": result[:500],
    })
    del RECENT_RULE_RUNS[:-RECENT_HISTORY_LIMIT]


def run_rule_step(rule_id: str, step: dict[str, Any], event: dict[str, Any]) -> tuple[bool, str]:
    action_type = str(step.get("action_type", "transcript"))
    if action_type == "action":
        action_name = resolve_script_action(str(step.get("action_name", ""))) or ""
        if not action_name:
            raise ValueError("action_name did not match a configured action")
        transcript = str(step.get("action_transcript") or f"run action {action_name}")
        result = run_script_action(action_name, str(event.get("device_id", "rule")), transcript)
        return bool(result.get("ok")), str(result.get("stdout") or result.get("stderr") or "")

    transcript = str(step.get("transcript", "")).strip()
    if not transcript:
        raise ValueError("transcript action is empty")
    response = command_response(transcript, str(event.get("device_id", "rule")))
    record = {
        "device_id": clean_device_id(str(event.get("device_id", "rule"))),
        "received_at": int(time.time()),
        "duration_ms": 0,
        "ok": bool(response.get("ok")),
        "text": transcript,
        "display_text": response["display_text"],
        "tone": response["tone"],
        "command": response.get("command"),
        "state": response.get("state", {}),
        "muted": MUTED_DEVICES.get(str(event.get("device_id", "rule")), False),
        "transcript": {"text": transcript, "source": "rule", "rule_id": rule_id},
    }
    record_command_result(record)
    return bool(response.get("ok")), str(response.get("display_text", ""))


def run_event_rule(rule_id: str, rule: dict[str, Any], event: dict[str, Any]) -> None:
    try:
        results = []
        ok = True
        for step in rule_steps(rule):
            step_ok, result = run_rule_step(rule_id, step, event)
            ok = ok and step_ok
            results.append(result)
        record_rule_run(rule_id, event, ok, " | ".join(item for item in results if item))
    except Exception as exc:
        record_rule_run(rule_id, event, False, str(exc))
        print(f"Rule {rule_id} failed: {exc}")


def run_matching_event_rules(event: dict[str, Any]) -> None:
    for rule_id, rule in list(EVENT_RULES.items()):
        if rule_matches_event(rule, event):
            run_event_rule(rule_id, dict(rule), dict(event))


def dispatch_server_event(event_type: str, device_id: str = "", **extra: Any) -> None:
    event = {
        "event_type": clean_rule_type(event_type),
        "device_id": clean_device_id(device_id) if device_id else "",
        "received_at": int(time.time()),
    }
    for key, value in extra.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            event[key] = value
    run_matching_event_rules(event)


def test_event_for_rule(rule: dict[str, Any]) -> dict[str, Any]:
    event_type = clean_rule_type(str(rule.get("event_type", "button")))
    event = {
        "event_type": event_type,
        "device_id": clean_device_id(str(rule.get("device_id", "rule-test"))),
        "received_at": int(time.time()),
    }
    if rule.get("button"):
        event["button"] = str(rule.get("button"))
    if rule.get("capability"):
        event["capability"] = str(rule.get("capability"))
    if rule.get("command"):
        event["command"] = str(rule.get("command"))
    if event_type == "low_battery":
        event.setdefault("battery_percent", 10)
    return event


def run_rule_test(rule_id: str) -> dict[str, Any]:
    rule = EVENT_RULES.get(rule_id)
    if not rule:
        raise ValueError("rule not found")
    event = test_event_for_rule(rule)
    run_event_rule(rule_id, dict(rule), event)
    return RECENT_RULE_RUNS[-1] if RECENT_RULE_RUNS else {}


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
        "device_type": infer_device_type(device_id, device),
        "firmware": device.get("firmware", {}),
        "firmware_version": device.get("firmware_version", ""),
        "firmware_project": device.get("firmware_project", ""),
        "capabilities": device.get("capabilities") or defaults.get("capabilities", []),
        "endpoints": device.get("endpoints", {}),
        "status": device.get("status", {}),
        "first_seen": device.get("first_seen"),
        "last_seen": device.get("last_seen"),
        "last_seen_via": device.get("last_seen_via", ""),
        "last_local_seen": device.get("last_local_seen"),
        "last_relay_seen": device.get("last_relay_seen"),
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


def touch_device(device_id: str, handler: BaseHTTPRequestHandler | None = None, source: str = "local") -> None:
    now = int(time.time())
    device = DEVICES.setdefault(device_id, {
        "id": device_id,
        "first_seen": now,
        "request_count": 0,
    })
    was_session_seen = bool(device.get("session_seen", False))
    for key, value in default_device_metadata(device_id).items():
        device.setdefault(key, value)
    if DEVICE_FRIENDLY_NAMES.get(device_id):
        device["friendly_name"] = DEVICE_FRIENDLY_NAMES[device_id]
    device["session_seen"] = True
    device["last_seen"] = now
    device["last_seen_via"] = source
    if source == "relay":
        device["last_relay_seen"] = now
    else:
        device["last_local_seen"] = now
    device["request_count"] = int(device.get("request_count", 0)) + 1
    if handler is not None:
        device["remote_addr"] = handler.client_address[0]
        device["user_agent"] = handler.headers.get("User-Agent", "")
    save_device_registry()
    if not was_session_seen:
        dispatch_server_event("device_online", device_id, device_type=str(device.get("type", "unknown")))


def register_device(device_id: str, payload: dict[str, Any], handler: BaseHTTPRequestHandler | None = None,
                    source: str = "local") -> dict[str, Any]:
    touch_device(device_id, handler, source)
    device = DEVICES[device_id]

    if "type" in payload:
        device["type"] = str(payload.get("type", "unknown"))[:32]
    if "model" in payload:
        device["model"] = str(payload.get("model", ""))[:80]
    if "device_type" in payload:
        device["device_type"] = clean_device_type(str(payload.get("device_type", "")))
    if isinstance(payload.get("firmware"), dict):
        firmware = {
            str(key)[:40]: value
            for key, value in payload["firmware"].items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
        device["firmware"] = firmware
        if firmware.get("version") is not None:
            device["firmware_version"] = str(firmware.get("version", ""))[:80]
        if firmware.get("project") is not None:
            device["firmware_project"] = str(firmware.get("project", ""))[:80]
        if firmware.get("device_type") is not None:
            device["device_type"] = clean_device_type(str(firmware.get("device_type", "")))
    if "firmware_version" in payload:
        device["firmware_version"] = str(payload.get("firmware_version", ""))[:80]
    if "firmware_project" in payload:
        device["firmware_project"] = str(payload.get("firmware_project", ""))[:80]
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
        maybe_dispatch_low_battery(device_id, device["status"])

    save_device_registry()
    return public_device(device_id)


def update_device_status(device_id: str, payload: dict[str, Any], handler: BaseHTTPRequestHandler | None = None,
                         source: str = "local") -> dict[str, Any]:
    touch_device(device_id, handler, source)
    device = DEVICES[device_id]
    raw_status = payload.get("status") if isinstance(payload.get("status"), dict) else payload
    if not isinstance(raw_status, dict):
        raw_status = {}
    status = dict(device.get("status", {})) if isinstance(device.get("status"), dict) else {}
    for key, value in raw_status.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            status[str(key)[:40]] = value
    device["status"] = status
    maybe_dispatch_low_battery(device_id, status)
    save_device_registry()
    return public_device(device_id)


def maybe_dispatch_low_battery(device_id: str, status: dict[str, Any]) -> None:
    raw_value = None
    for key in ("battery_percent", "battery_pct", "battery_level", "battery"):
        if isinstance(status.get(key), (int, float)):
            raw_value = float(status[key])
            break
    if raw_value is None:
        return
    if raw_value > 1:
        percent = raw_value
    else:
        percent = raw_value * 100
    if percent <= LOW_BATTERY_THRESHOLD_PERCENT:
        if device_id not in LOW_BATTERY_NOTIFIED:
            LOW_BATTERY_NOTIFIED.add(device_id)
            dispatch_server_event("low_battery", device_id, battery_percent=round(percent, 1))
    elif percent > LOW_BATTERY_THRESHOLD_PERCENT + 5:
        LOW_BATTERY_NOTIFIED.discard(device_id)


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


def clean_monitor_target(target: str) -> str:
    value = target.strip()[:240]
    if not value:
        raise ValueError("target is required")
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        if not parsed.netloc:
            raise ValueError("URL target must include a host")
        return value
    if re.search(r"\s", value):
        raise ValueError("host target cannot contain spaces")
    return value


def public_uptime_monitor(monitor: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time())
    result = dict(monitor)
    result["due"] = bool(result.get("enabled", True)) and int(result.get("next_check_at", 0) or 0) <= now
    return result


def save_uptime_monitors() -> None:
    directory = os.path.dirname(UPTIME_MONITORS_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = {"monitors": [UPTIME_MONITORS[monitor_id] for monitor_id in sorted(UPTIME_MONITORS)]}
    temp_path = f"{UPTIME_MONITORS_PATH}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    os.replace(temp_path, UPTIME_MONITORS_PATH)


def load_uptime_monitors() -> None:
    try:
        with open(UPTIME_MONITORS_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return
    except Exception as exc:
        print(f"Could not load uptime monitors: {exc}")
        return

    now = int(time.time())
    for raw in payload.get("monitors", []) if isinstance(payload, dict) else []:
        if not isinstance(raw, dict):
            continue
        try:
            monitor_id = clean_rule_id(str(raw.get("id", "")) or uuid.uuid4().hex)
            target = clean_monitor_target(str(raw.get("target", "")))
            interval = max(30, int(raw.get("interval_seconds", 600) or 600))
        except Exception:
            continue
        UPTIME_MONITORS[monitor_id] = {
            "id": monitor_id,
            "name": clean_friendly_name(str(raw.get("name", ""))) or target,
            "target": target,
            "interval_seconds": interval,
            "enabled": bool(raw.get("enabled", True)),
            "created_at": int(raw.get("created_at", now) or now),
            "last_checked_at": int(raw.get("last_checked_at", 0) or 0),
            "next_check_at": min(int(raw.get("next_check_at", now) or now), now),
            "online": bool(raw.get("online", False)),
            "detail": str(raw.get("detail", "not checked"))[:240],
            "latency_ms": raw.get("latency_ms"),
            "status_code": raw.get("status_code"),
        }


def upsert_uptime_monitor(payload: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time())
    monitor_id = clean_rule_id(str(payload.get("id", "")) or uuid.uuid4().hex)
    target = clean_monitor_target(str(payload.get("target", "")))
    interval = max(30, int(payload.get("interval_seconds", 600) or 600))
    existing = UPTIME_MONITORS.get(monitor_id, {})
    monitor = {
        "id": monitor_id,
        "name": clean_friendly_name(str(payload.get("name", ""))) or str(existing.get("name", "")) or target,
        "target": target,
        "interval_seconds": interval,
        "enabled": bool(payload.get("enabled", existing.get("enabled", True))),
        "created_at": int(existing.get("created_at", now)),
        "last_checked_at": int(existing.get("last_checked_at", 0) or 0),
        "next_check_at": now,
        "online": bool(existing.get("online", False)),
        "detail": str(existing.get("detail", "not checked"))[:240],
        "latency_ms": existing.get("latency_ms"),
        "status_code": existing.get("status_code"),
    }
    UPTIME_MONITORS[monitor_id] = monitor
    save_uptime_monitors()
    return public_uptime_monitor(monitor)


def check_uptime_monitor(monitor: dict[str, Any]) -> dict[str, Any]:
    target = str(monitor.get("target", ""))
    started = time.monotonic()
    if target.startswith(("http://", "https://")):
        try:
            request = Request(target, method="GET", headers={"User-Agent": "SpokenCommandServer/0.1"})
            with urlopen(request, timeout=5) as response:
                latency_ms = int((time.monotonic() - started) * 1000)
                return {
                    "online": response.status < 500,
                    "detail": f"http {response.status}",
                    "latency_ms": latency_ms,
                    "status_code": response.status,
                }
        except HTTPError as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            return {
                "online": exc.code < 500,
                "detail": f"http {exc.code}",
                "latency_ms": latency_ms,
                "status_code": exc.code,
            }
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            return {"online": False, "detail": str(exc)[:240], "latency_ms": latency_ms, "status_code": None}

    ping_target = target
    if target.endswith(".local"):
        try:
            resolved = subprocess.run(
                ["avahi-resolve-host-name", "-4", target],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                check=False,
            )
            if resolved.returncode == 0:
                parts = resolved.stdout.strip().split()
                if len(parts) >= 2:
                    ping_target = parts[1]
            else:
                detail = resolved.stderr.strip() or resolved.stdout.strip() or "mDNS resolution failed"
                return {"online": False, "detail": detail[:240], "latency_ms": None, "status_code": None}
        except FileNotFoundError:
            return {"online": False, "detail": "avahi-resolve-host-name not installed", "latency_ms": None, "status_code": None}
        except Exception as exc:
            return {"online": False, "detail": f"mDNS resolution failed: {exc}"[:240], "latency_ms": None, "status_code": None}

    try:
        completed = subprocess.run(
            ["ping", "-c", "1", "-W", "3", ping_target],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
        latency_ms = None
        match = re.search(r"time=([0-9.]+)\s*ms", completed.stdout)
        if match:
            latency_ms = int(float(match.group(1)))
        detail = "ping ok" if completed.returncode == 0 else (completed.stderr.strip() or completed.stdout.strip() or "ping failed")
        return {"online": completed.returncode == 0, "detail": detail[:240], "latency_ms": latency_ms, "status_code": None}
    except Exception as exc:
        return {"online": False, "detail": str(exc)[:240], "latency_ms": None, "status_code": None}


def uptime_worker() -> None:
    while True:
        with STATE_LOCK:
            now = int(time.time())
            due = [
                dict(monitor)
                for monitor in UPTIME_MONITORS.values()
                if monitor.get("enabled", True) and int(monitor.get("next_check_at", now) or now) <= now
            ]

        for monitor in due:
            result = check_uptime_monitor(monitor)
            with STATE_LOCK:
                current = UPTIME_MONITORS.get(str(monitor.get("id", "")))
                if not current:
                    continue
                now = int(time.time())
                current.update(result)
                current["last_checked_at"] = now
                current["next_check_at"] = now + max(30, int(current.get("interval_seconds", 600) or 600))
                save_uptime_monitors()

        time.sleep(5)


def read_cpu_times() -> dict[str, int] | None:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as handle:
            first = handle.readline().split()
    except OSError:
        return None
    if not first or first[0] != "cpu":
        return None
    values = [int(value) for value in first[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return {"idle": idle, "total": total}


def cpu_usage_percent() -> float | None:
    global LAST_CPU_SAMPLE
    current = read_cpu_times()
    if not current:
        return None
    previous = LAST_CPU_SAMPLE
    LAST_CPU_SAMPLE = current
    if not previous:
        return None
    total_delta = current["total"] - previous["total"]
    idle_delta = current["idle"] - previous["idle"]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, (1.0 - (idle_delta / total_delta)) * 100.0)), 1)


def memory_info() -> dict[str, Any]:
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2:
                    values[parts[0].rstrip(":")] = int(parts[1]) * 1024
    except OSError:
        return {}

    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    swap_total = values.get("SwapTotal")
    swap_free = values.get("SwapFree")
    used = total - available if total is not None and available is not None else None
    swap_used = swap_total - swap_free if swap_total is not None and swap_free is not None else None
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": used,
        "used_percent": round((used / total) * 100, 1) if total and used is not None else None,
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_used,
        "swap_used_percent": round((swap_used / swap_total) * 100, 1) if swap_total and swap_used is not None else None,
    }


def disk_info(path: str, label: str) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return {"label": label, "path": path, "error": str(exc)}
    used = usage.total - usage.free
    return {
        "label": label,
        "path": path,
        "total_bytes": usage.total,
        "used_bytes": used,
        "free_bytes": usage.free,
        "used_percent": round((used / usage.total) * 100, 1) if usage.total else None,
    }


def gpu_info_from_nvidia_smi() -> list[dict[str, Any]]:
    if not shutil.which("nvidia-smi"):
        return []
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1.5,
            check=False,
        )
    except Exception:
        return []
    if completed.returncode != 0:
        return []
    gpus = []
    for index, line in enumerate(completed.stdout.splitlines()):
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5:
            continue
        memory_used = int(parts[2]) * 1024 * 1024 if parts[2].isdigit() else None
        memory_total = int(parts[3]) * 1024 * 1024 if parts[3].isdigit() else None
        gpus.append({
            "id": f"nvidia-{index}",
            "name": parts[0],
            "provider": "nvidia-smi",
            "usage_percent": float(parts[1]) if parts[1].replace(".", "", 1).isdigit() else None,
            "memory_used_bytes": memory_used,
            "memory_total_bytes": memory_total,
            "memory_used_percent": round((memory_used / memory_total) * 100, 1) if memory_used is not None and memory_total else None,
            "temperature_c": int(parts[4]) if parts[4].isdigit() else None,
        })
    return gpus


def gpu_info_from_drm() -> list[dict[str, Any]]:
    gpus = []
    for card_path in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        busy_path = os.path.join(card_path, "device", "gpu_busy_percent")
        if not os.path.exists(busy_path):
            continue
        try:
            with open(busy_path, "r", encoding="utf-8") as handle:
                usage = float(handle.read().strip())
        except OSError:
            continue
        name = os.path.basename(card_path)
        vendor_path = os.path.join(card_path, "device", "vendor")
        device_path = os.path.join(card_path, "device", "device")
        vendor = ""
        device = ""
        try:
            with open(vendor_path, "r", encoding="utf-8") as handle:
                vendor = handle.read().strip()
            with open(device_path, "r", encoding="utf-8") as handle:
                device = handle.read().strip()
        except OSError:
            pass
        gpus.append({
            "id": name,
            "name": f"{name} {vendor} {device}".strip(),
            "provider": "drm",
            "usage_percent": round(usage, 1),
        })
    return gpus


def load_average() -> dict[str, float] | None:
    try:
        one, five, fifteen = os.getloadavg()
    except OSError:
        return None
    return {"1m": round(one, 2), "5m": round(five, 2), "15m": round(fifteen, 2)}


def system_info_snapshot() -> dict[str, Any]:
    global LAST_SYSTEM_INFO, LAST_SYSTEM_INFO_AT
    now = time.monotonic()
    if LAST_SYSTEM_INFO is not None and now - LAST_SYSTEM_INFO_AT < 2.0:
        return LAST_SYSTEM_INFO

    server_dir = os.path.dirname(os.path.realpath(__file__))
    gpus = gpu_info_from_nvidia_smi() or gpu_info_from_drm()
    info = {
        "collected_at": int(time.time()),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu": {
            "usage_percent": cpu_usage_percent(),
            "load_average": load_average(),
            "core_count": os.cpu_count(),
        },
        "memory": memory_info(),
        "storage": [
            disk_info("/", "Root"),
            disk_info(server_dir, "Server directory"),
        ],
        "gpus": gpus,
        "gpu_note": "" if gpus else "No GPU telemetry detected",
    }
    LAST_SYSTEM_INFO = info
    LAST_SYSTEM_INFO_AT = now
    return info


def relay_safe_system_info(value: Any, key: str = "") -> Any:
    lowered_key = key.lower()
    if "ip" in lowered_key or "addr" in lowered_key or "address" in lowered_key:
        return None
    if isinstance(value, dict):
        result = {}
        for child_key, child_value in value.items():
            cleaned = relay_safe_system_info(child_value, str(child_key))
            if cleaned is not None:
                result[child_key] = cleaned
        return result
    if isinstance(value, list):
        return [
            cleaned
            for item in value
            if (cleaned := relay_safe_system_info(item, key)) is not None
        ]
    if isinstance(value, str):
        redacted = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[redacted-ip]", value)
        if re.fullmatch(r"[0-9a-fA-F:]{3,}", redacted) and ":" in redacted:
            return "[redacted-ip]"
        return redacted
    return value


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
            "diagnostics": startup_diagnostics(),
            "restart_enabled": RESTART_ENABLED,
            "system": system_info_snapshot(),
        },
        "summary": {
            "device_count": len(devices),
            "online_count": online_count,
            "offline_count": len(devices) - online_count,
            "pending_event_count": sum(int(device.get("pending_events", 0)) for device in devices),
            "active_timer_count": len(ACTIVE_TIMERS),
            "rule_count": len(EVENT_RULES),
            "recent_command_count": len(RECENT_COMMANDS),
            "recent_button_event_count": len(RECENT_BUTTON_EVENTS),
            "device_types": device_types,
            "capabilities": capability_counts,
        },
        "firmware_catalog": firmware_catalog_summary(),
        "actions": list_script_actions(),
        "rules": [public_rule(rule_id, EVENT_RULES[rule_id]) for rule_id in sorted(EVENT_RULES)],
        "recent_rule_runs": RECENT_RULE_RUNS[-20:],
        "uptime_monitors": [public_uptime_monitor(UPTIME_MONITORS[monitor_id]) for monitor_id in sorted(UPTIME_MONITORS)],
        "mission_board": mission_board_summary(),
        "r1_note": public_r1_note(),
        "active_timers": active_timer_summary(),
        "devices": devices,
        "recent_commands": RECENT_COMMANDS[-20:],
        "recent_button_events": RECENT_BUTTON_EVENTS[-20:],
    }


def external_dashboard_snapshot() -> dict[str, Any]:
    snapshot = dashboard_snapshot()
    external = {
        "server": {
            "started_at": snapshot["server"].get("started_at"),
            "uptime_seconds": snapshot["server"].get("uptime_seconds"),
            "device_stale_seconds": snapshot["server"].get("device_stale_seconds"),
            "system": relay_safe_system_info(snapshot["server"].get("system", {})),
        },
        "summary": snapshot.get("summary", {}),
        "devices": [],
        "uptime_monitors": snapshot.get("uptime_monitors", []),
        "mission_board": snapshot.get("mission_board", {}),
        "r1_note": snapshot.get("r1_note", {}),
        "recent_button_events": snapshot.get("recent_button_events", []),
        "recent_rule_runs": snapshot.get("recent_rule_runs", []),
    }

    for raw_device in snapshot.get("devices", []):
        if not isinstance(raw_device, dict):
            continue
        device = {
            key: raw_device.get(key)
            for key in (
                "id",
                "display_name",
                "friendly_name",
                "type",
                "model",
                "device_type",
                "firmware",
                "firmware_version",
                "firmware_project",
                "capabilities",
                "status",
                "first_seen",
                "last_seen",
                "last_seen_via",
                "last_local_seen",
                "last_relay_seen",
                "online",
                "online_detail",
                "age_seconds",
                "pending_events",
            )
            if key in raw_device
        }
        external["devices"].append(device)

    return external


def relay_request_json(path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not RELAY_URL:
        raise RuntimeError("COMMAND_SERVER_RELAY_URL is not configured")
    if not RELAY_SYNC_TOKEN:
        raise RuntimeError("COMMAND_SERVER_RELAY_SYNC_TOKEN is not configured")

    body = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {RELAY_SYNC_TOKEN}",
        "User-Agent": "SpokenCommandServer/0.1",
    }
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(f"{RELAY_URL}{path}", data=body, headers=headers, method=method)
    with urlopen(request, timeout=RELAY_TIMEOUT_SECONDS) as response:
        data = response.read()
    if not data:
        return {}
    parsed = json.loads(data.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("relay response must be a JSON object")
    return parsed


def relay_pairing_request_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not RELAY_PAIRING_URL:
        raise RuntimeError("COMMAND_SERVER_RELAY_PAIRING_URL or COMMAND_SERVER_RELAY_URL is not configured")
    if not RELAY_IP_PAIRING_TOKEN:
        raise RuntimeError("COMMAND_SERVER_RELAY_IP_PAIRING_TOKEN is not configured")

    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {RELAY_IP_PAIRING_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "SpokenCommandServerPairing/0.1",
    }
    request = Request(f"{RELAY_PAIRING_URL}{path}", data=body, headers=headers, method="POST")
    with urlopen(request, timeout=RELAY_TIMEOUT_SECONDS) as response:
        data = response.read()
    if not data:
        return {}
    parsed = json.loads(data.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("relay pairing response must be a JSON object")
    return parsed


def split_csv_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def discover_local_ips() -> list[str]:
    configured = split_csv_values(RELAY_PAIRING_LOCAL_IPS)
    if configured:
        return configured

    addresses: set[str] = set()
    hostname = socket.gethostname()
    try:
        for family, _, _, _, sockaddr in socket.getaddrinfo(hostname, None):
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            address = str(sockaddr[0])
            if address.startswith("127.") or address == "::1" or address.startswith("fe80:"):
                continue
            addresses.add(address)
    except OSError:
        pass

    probes = (
        (socket.AF_INET, ("8.8.8.8", 80)),
        (socket.AF_INET6, ("2001:4860:4860::8888", 80)),
    )
    for family, target in probes:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as probe:
                probe.connect(target)
                address = str(probe.getsockname()[0])
                if not address.startswith("127.") and address != "::1" and not address.startswith("fe80:"):
                    addresses.add(address)
        except OSError:
            pass

    return sorted(addresses)


def relay_pairing_payload() -> dict[str, Any]:
    return {
        "name": RELAY_PAIRING_NAME,
        "type": RELAY_PAIRING_TYPE,
        "hostname": socket.gethostname(),
        "local_ips": discover_local_ips(),
        "ports": split_csv_values(RELAY_PAIRING_PORTS),
        "notes": RELAY_PAIRING_NOTES,
    }


def relay_pairing_worker() -> None:
    if not RELAY_PAIRING_ENABLED:
        return
    if not RELAY_PAIRING_URL or not RELAY_IP_PAIRING_TOKEN:
        print(
            "Relay pairing disabled: "
            "COMMAND_SERVER_RELAY_PAIRING_URL/COMMAND_SERVER_RELAY_URL "
            "or COMMAND_SERVER_RELAY_IP_PAIRING_TOKEN is missing."
        )
        return

    device_id = clean_device_id(RELAY_PAIRING_DEVICE_ID)
    while True:
        try:
            relay_pairing_request_json(f"/paired-devices/{device_id}", relay_pairing_payload())
        except Exception as exc:
            print(f"Relay pairing update failed: {exc}")
        time.sleep(RELAY_PAIRING_SECONDS)


def relay_ack_event(event_id: str, ok: bool, error: str = "") -> None:
    relay_request_json(
        f"/sync/events/{event_id}/ack",
        method="POST",
        payload={"ok": ok, "error": error[:240]},
    )


def process_relay_event(event: dict[str, Any]) -> None:
    event_id = clean_rule_id(str(event.get("id", "")))
    event_type = clean_rule_type(str(event.get("event_type", "")))
    device_id = clean_device_id(str(event.get("device_id", "")))
    payload = event.get("payload")
    if not event_id:
        raise ValueError("relay event id is required")
    if not device_id:
        raise ValueError("relay event device_id is required")
    if not isinstance(payload, dict):
        payload = {}

    if event_type == "register":
        register_device(device_id, payload, source="relay")
        return
    if event_type == "status":
        update_device_status(device_id, payload, source="relay")
        return
    if event_type == "button":
        record_button_event(device_id, payload, source="relay")
        return
    if event_type == "mission_task_create":
        create_mission_task(payload, source="relay")
        return
    if event_type == "mission_task_complete":
        complete_mission_task(str(payload.get("id", "")), completed_by=str(payload.get("completed_by", "relay")))
        return
    raise ValueError(f"unsupported relay event type: {event_type}")


def process_relay_device_statuses(devices: list[Any]) -> int:
    processed = 0
    for raw_device in devices:
        if not isinstance(raw_device, dict):
            continue
        device_id = clean_device_id(str(raw_device.get("id", "")))
        if not device_id:
            continue
        payload = raw_device.get("status") if isinstance(raw_device.get("status"), dict) else {}
        update_device_status(device_id, {"status": payload}, source="relay")
        device = DEVICES.get(device_id, {})
        if isinstance(raw_device.get("last_seen"), (int, float)):
            relay_seen = int(raw_device["last_seen"])
            current_seen = device.get("last_seen")
            if not isinstance(current_seen, (int, float)) or relay_seen > int(current_seen):
                device["last_seen"] = relay_seen
            current_relay_seen = device.get("last_relay_seen")
            if not isinstance(current_relay_seen, (int, float)) or relay_seen > int(current_relay_seen):
                device["last_relay_seen"] = relay_seen
        if raw_device.get("remote_addr"):
            device["remote_addr"] = str(raw_device.get("remote_addr", ""))[:120]
        if raw_device.get("user_agent"):
            device["user_agent"] = str(raw_device.get("user_agent", ""))[:240]
        processed += 1
    if processed:
        save_device_registry()
    return processed


def relay_sync_worker() -> None:
    if not RELAY_ENABLED:
        return
    if not RELAY_URL or not RELAY_SYNC_TOKEN:
        print("Relay sync disabled: COMMAND_SERVER_RELAY_URL or COMMAND_SERVER_RELAY_SYNC_TOKEN is missing.")
        return

    next_snapshot_at = 0.0
    while True:
        now = time.monotonic()
        try:
            if now >= next_snapshot_at:
                with STATE_LOCK:
                    snapshot = external_dashboard_snapshot()
                relay_request_json("/sync/dashboard-snapshot", method="POST", payload=snapshot)
                relay_request_json("/sync/r1-note", method="POST", payload=snapshot.get("r1_note", {}))
                next_snapshot_at = now + RELAY_SNAPSHOT_SECONDS

            status_payload = relay_request_json("/sync/device-statuses")
            status_devices = status_payload.get("devices", [])
            if isinstance(status_devices, list):
                with STATE_LOCK:
                    process_relay_device_statuses(status_devices)

            payload = relay_request_json("/sync/events")
            events = payload.get("events", [])
            if not isinstance(events, list):
                events = []
            for raw_event in events:
                if not isinstance(raw_event, dict):
                    continue
                event_id = clean_rule_id(str(raw_event.get("id", "")))
                try:
                    with STATE_LOCK:
                        process_relay_event(raw_event)
                    if event_id:
                        relay_ack_event(event_id, True)
                except Exception as exc:
                    print(f"Relay event failed: id={event_id} error={exc}")
                    if event_id:
                        try:
                            relay_ack_event(event_id, False, str(exc))
                        except Exception as ack_exc:
                            print(f"Relay event ack failed: id={event_id} error={ack_exc}")
        except Exception as exc:
            print(f"Relay sync failed: {exc}")

        time.sleep(RELAY_POLL_SECONDS)


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


def record_button_event(device_id: str, payload: dict[str, Any], handler: BaseHTTPRequestHandler | None = None,
                        source: str = "local") -> dict[str, Any]:
    touch_device(device_id, handler, source)
    event = {
        "device_id": device_id,
        "event_type": "button",
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
    del RECENT_BUTTON_EVENTS[:-RECENT_HISTORY_LIMIT]
    save_button_event_record(event)

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
    run_matching_event_rules(event)
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


def parse_timer_name(text: str) -> str:
    normalized = " ".join(text.strip().split())
    patterns = [
        r"\bnamed\s+(.+?)(?=\s+(?:for|in)\b|$)",
        r"\bcalled\s+(.+?)(?=\s+(?:for|in)\b|$)",
        r"\bname\s+(.+?)(?=\s+(?:for|in)\b|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return clean_friendly_name(match.group(1))
    match = re.search(r"\bset\s+(?:a\s+)?(.+?)\s+timer\b", normalized, flags=re.IGNORECASE)
    if match:
        candidate = clean_friendly_name(match.group(1))
        if candidate and candidate.lower() not in {"the", "a"}:
            return candidate
    match = re.search(r"\bstart\s+(?:a\s+)?(.+?)\s+timer\b", normalized, flags=re.IGNORECASE)
    if match:
        candidate = clean_friendly_name(match.group(1))
        if candidate and candidate.lower() not in {"the", "a"}:
            return candidate
    return ""


def parse_timer_request(text: str) -> tuple[int | None, str]:
    normalized = normalize_command_text(text)
    seconds = parse_duration_seconds(normalized)
    if "notify phone" in normalized or "phone notification" in normalized or "notification" in normalized:
        mode = "phone"
    elif "all devices" in normalized or "standard devices" in normalized or "alert devices" in normalized or "broadcast" in normalized:
        mode = "all_devices"
    else:
        mode = "device"
    return seconds, mode


def timer_mode_label(mode: str) -> str:
    if mode == "phone":
        return "phone notification"
    if mode == "all_devices":
        return "all devices"
    return "this device"


def clean_timer_mode(mode: str) -> str:
    if mode in {"phone", "all_devices", "device"}:
        return mode
    return "device"


def timer_display_name(timer: dict[str, Any]) -> str:
    name = str(timer.get("name", "")).strip()
    return name or "Timer"


def save_timers() -> None:
    directory = os.path.dirname(TIMER_STATE_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = {"timers": [ACTIVE_TIMERS[timer_id] for timer_id in sorted(ACTIVE_TIMERS)]}
    temp_path = f"{TIMER_STATE_PATH}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    os.replace(temp_path, TIMER_STATE_PATH)


def load_timers() -> None:
    try:
        with open(TIMER_STATE_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return
    except Exception as exc:
        print(f"Could not load timers: {exc}")
        return

    now = int(time.time())
    for raw_timer in payload.get("timers", []) if isinstance(payload, dict) else []:
        if not isinstance(raw_timer, dict):
            continue
        timer_id = clean_rule_id(str(raw_timer.get("id", "")) or uuid.uuid4().hex)
        expires_at = int(raw_timer.get("expires_at", 0) or 0)
        if expires_at <= now:
            continue
        ACTIVE_TIMERS[timer_id] = {
            "id": timer_id,
            "device_id": clean_device_id(str(raw_timer.get("device_id", "unknown"))),
            "name": clean_friendly_name(str(raw_timer.get("name", ""))),
            "created_at": int(raw_timer.get("created_at", now)),
            "expires_at": expires_at,
            "duration_seconds": int(raw_timer.get("duration_seconds", max(1, expires_at - now))),
            "mode": clean_timer_mode(str(raw_timer.get("mode", "device"))),
            "transcript": str(raw_timer.get("transcript", ""))[:500],
        }


def schedule_timer(device_id: str, transcript: str, seconds: int, mode: str, name: str = "") -> dict[str, Any]:
    timer_id = uuid.uuid4().hex
    now = int(time.time())
    timer = {
        "id": timer_id,
        "device_id": device_id,
        "name": clean_friendly_name(name),
        "created_at": now,
        "expires_at": now + seconds,
        "duration_seconds": seconds,
        "mode": clean_timer_mode(mode),
        "transcript": transcript,
    }
    with TIMER_CONDITION:
        ACTIVE_TIMERS[timer_id] = timer
        save_timers()
        TIMER_CONDITION.notify_all()
    return timer


def active_timer_summary() -> list[dict[str, Any]]:
    now = int(time.time())
    return [
        {
            **timer,
            "display_name": timer_display_name(timer),
            "remaining_seconds": max(0, int(timer.get("expires_at", now)) - now),
            "device_name": device_display_name(str(timer.get("device_id", "")), DEVICES.get(str(timer.get("device_id", "")), {})),
        }
        for timer in sorted(ACTIVE_TIMERS.values(), key=lambda item: int(item.get("expires_at", 0)))
    ]


def cancel_timer(reference: str, device_id: str = "") -> dict[str, Any] | None:
    with TIMER_CONDITION:
        normalized_ref = normalize_command_text(reference)
        matches = []
        for timer_id, timer in ACTIVE_TIMERS.items():
            if device_id and str(timer.get("device_id", "")) != device_id:
                continue
            names = [timer_id, str(timer.get("name", "")), str(timer.get("transcript", ""))]
            if not normalized_ref:
                matches.append((0, timer_id))
                continue
            for name in names:
                normalized_name = normalize_command_text(name)
                if normalized_name and (normalized_ref == normalized_name or normalized_ref in normalized_name):
                    matches.append((len(normalized_name), timer_id))
                    break
        if not matches:
            return None
        matches.sort(reverse=True)
        timer_id = matches[0][1]
        timer = ACTIVE_TIMERS.pop(timer_id, None)
        save_timers()
        TIMER_CONDITION.notify_all()
        return timer


def fire_timer(timer: dict[str, Any]) -> None:
    device_id = str(timer.get("device_id", "unknown"))
    mode = str(timer.get("mode", "device"))
    duration = format_duration(int(timer.get("duration_seconds", 0)))
    message = f"Timer done: {duration}"
    dispatch_server_event("timer_complete", device_id, timer_id=str(timer.get("id", "")), mode=mode, duration_seconds=timer.get("duration_seconds"))

    if mode == "phone":
        try:
            run_script_action("notify phone", device_id, f"run action notify phone message {message}")
        except Exception as exc:
            print(f"Timer notification failed: {exc}")
        return

    targets = event_capable_device_ids() if mode == "all_devices" else [device_id]
    for target_id in targets:
        enqueue_device_event(target_id, "alert", message, "alert", source_device_id=device_id)


def timer_worker() -> None:
    while True:
        with TIMER_CONDITION:
            now = int(time.time())
            due = [
                ACTIVE_TIMERS.pop(timer_id)
                for timer_id, timer in list(ACTIVE_TIMERS.items())
                if int(timer.get("expires_at", now + 1)) <= now
            ]
            if due:
                save_timers()
            if not due:
                next_expiry = min((int(timer.get("expires_at", now + 60)) for timer in ACTIVE_TIMERS.values()), default=now + 60)
                TIMER_CONDITION.wait(timeout=max(1, min(60, next_expiry - now)))
                continue

        for timer in due:
            try:
                with STATE_LOCK:
                    fire_timer(timer)
                    save_device_registry()
            except Exception as exc:
                print(f"Timer failed: {exc}")


def timer_response(transcript: str, device_id: str, duration_text: str, mode: str = "device", name: str = "") -> dict[str, Any]:
    seconds, parsed_mode = parse_timer_request(duration_text)
    mode = parsed_mode if parsed_mode != "device" else mode
    name = clean_friendly_name(name or parse_timer_name(duration_text))
    if seconds is None or seconds <= 0:
        PENDING_ACTIONS[device_id] = {
            "command": "timer",
            "slot": "duration",
            "mode": mode,
            "name": name,
            "prompt": "How long should the timer be?",
            "created_at": time.time(),
        }
        return apply_mute_state(device_id, base_response(
            False,
            transcript,
            "How long should the timer be?",
            "error",
            command="timer",
            state={"awaiting": "duration", "mode": mode, "name": name},
        ))

    timer = schedule_timer(device_id, transcript, seconds, mode, name)
    label = f"{name}: " if name else ""
    return apply_mute_state(device_id, base_response(
        True,
        transcript,
        f"Timer set: {label}{format_duration(seconds)} -> {timer_mode_label(mode)}",
        "success",
        command="timer",
        state=timer,
    ))


def create_timer_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    device_id = clean_device_id(str(payload.get("device_id", "dashboard") or "dashboard"))
    name = clean_friendly_name(str(payload.get("name", "")))
    mode = clean_timer_mode(str(payload.get("mode", "device")))
    duration_text = str(payload.get("duration_text", "")).strip()
    seconds = None
    if isinstance(payload.get("duration_seconds"), (int, float)):
        seconds = int(payload["duration_seconds"])
    if seconds is None and duration_text:
        seconds, parsed_mode = parse_timer_request(duration_text)
        if parsed_mode != "device":
            mode = parsed_mode
        if not name:
            name = parse_timer_name(duration_text)
    if seconds is None or seconds <= 0:
        raise ValueError("duration_seconds or duration_text is required")
    transcript = str(payload.get("transcript", "") or f"dashboard timer {name or format_duration(seconds)}")[:500]
    return schedule_timer(device_id, transcript, seconds, mode, name)


def handle_pending_action(device_id: str, text: str, normalized: str) -> dict[str, Any] | None:
    if normalized in {"cancel", "stop", "nevermind", "never mind"}:
        PENDING_ACTIONS.pop(device_id, None)
        return apply_mute_state(device_id, base_response(True, text, "Cancelled.", "success", command="cancel"))

    pending = PENDING_ACTIONS.get(device_id)
    if pending is None:
        return None

    if pending.get("command") == "timer" and pending.get("slot") == "duration":
        response = timer_response(text, device_id, text, str(pending.get("mode", "device")), str(pending.get("name", "")))
        if response.get("ok"):
            PENDING_ACTIONS.pop(device_id, None)
        return response

    if pending.get("command") == "script_action" and pending.get("slot") == "confirmation":
        if normalized not in {"yes", "confirm", "confirmed", "do it", "run it", "go ahead"}:
            return apply_mute_state(device_id, base_response(
                False,
                text,
                "Please say confirm or cancel.",
                "error",
                command="script_action",
                state={"awaiting": "confirmation", "action": pending.get("action_name")},
            ))
        PENDING_ACTIONS.pop(device_id, None)
        action_name = str(pending.get("action_name", ""))
        return script_action_response(text, device_id, action_name)

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
        "Commands: test, status, list devices, ping, mute, broadcast, timer, list timers, cancel timer, run action.",
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
    for result in offline:
        dispatch_server_event("device_offline", str(result.get("id", "")), detail=str(result.get("detail", "")))

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
    _seconds, mode = parse_timer_request(duration_text or text)
    name = parse_timer_name(duration_text or text)
    if not duration_text:
        PENDING_ACTIONS[device_id] = {
            "command": "timer",
            "slot": "duration",
            "mode": mode,
            "name": name,
            "prompt": "How long should the timer be?",
            "created_at": time.time(),
        }
        return apply_mute_state(device_id, base_response(
            True,
            text,
            "How long should the timer be?",
            "success",
            command="timer",
            state={"awaiting": "duration", "mode": mode, "name": name},
        ))
    return timer_response(text, device_id, duration_text, mode, name)


def handle_list_timers(text: str, device_id: str, _remainder: str) -> dict[str, Any]:
    timers = active_timer_summary()
    if not timers:
        return apply_mute_state(device_id, base_response(
            True,
            text,
            "No active timers.",
            "success",
            command="list_timers",
            state={"timers": []},
        ))

    lines = []
    for timer in timers[:6]:
        label = str(timer.get("display_name", "Timer"))
        remaining = format_duration(int(timer.get("remaining_seconds", 0)))
        device_name = str(timer.get("device_name", timer.get("device_id", "")))
        lines.append(f"{label}: {remaining} ({device_name})")
    if len(timers) > len(lines):
        lines.append(f"+{len(timers) - len(lines)} more")

    return apply_mute_state(device_id, base_response(
        True,
        text,
        "\n".join(lines),
        "success",
        command="list_timers",
        state={"timers": timers},
    ))


def handle_cancel_timer(text: str, device_id: str, remainder: str) -> dict[str, Any]:
    reference = remainder.strip()
    normalized = normalize_command_text(reference)
    if normalized.endswith(" timer"):
        reference = reference[: -len(" timer")].strip()
    timer = cancel_timer(reference, device_id)
    if timer is None and reference:
        timer = cancel_timer(reference, "")
    if timer is None:
        return apply_mute_state(device_id, base_response(
            False,
            text,
            "Timer not found.",
            "error",
            command="cancel_timer",
            state={"query": reference},
        ))

    label = timer_display_name(timer)
    duration = format_duration(max(0, int(timer.get("expires_at", 0)) - int(time.time())))
    return apply_mute_state(device_id, base_response(
        True,
        text,
        f"Cancelled {label}: {duration} remaining.",
        "success",
        command="cancel_timer",
        state={"timer": timer},
    ))


def script_action_response(text: str, device_id: str, action_name: str) -> dict[str, Any]:
    try:
        result = run_script_action(action_name, device_id, text)
    except subprocess.TimeoutExpired:
        return apply_mute_state(device_id, base_response(
            False,
            text,
            "Action timed out.",
            "error",
            command="script_action",
            state={"action": action_name},
        ))
    except Exception as exc:
        return apply_mute_state(device_id, base_response(
            False,
            text,
            "Action failed.",
            "error",
            command="script_action",
            state={"action": action_name, "error": str(exc)},
        ))

    display_output = result["stdout"] or result["stderr"] or ("Action complete." if result["ok"] else "Action failed.")
    first_line = display_output.splitlines()[0] if display_output else ""
    display_text = first_line[:160] or ("Action complete." if result["ok"] else "Action failed.")
    return apply_mute_state(device_id, base_response(
        bool(result["ok"]),
        text,
        display_text,
        "success" if result["ok"] else "error",
        command="script_action",
        state={"action": action_name, "result": result},
    ))


def handle_script_action(text: str, device_id: str, remainder: str) -> dict[str, Any]:
    if not SCRIPT_ACTIONS:
        return apply_mute_state(device_id, base_response(
            False,
            text,
            "No server actions configured.",
            "error",
            command="script_action",
        ))

    action_name = resolve_script_action(remainder)
    if not action_name:
        available = ", ".join(action["name"] for action in list_script_actions()[:4])
        return apply_mute_state(device_id, base_response(
            False,
            text,
            f"Available actions: {available}" if available else "No server actions configured.",
            "error",
            command="script_action",
            state={"available_actions": list_script_actions()},
        ))

    action = SCRIPT_ACTIONS[action_name]
    if action.get("requires_confirmation"):
        PENDING_ACTIONS[device_id] = {
            "command": "script_action",
            "slot": "confirmation",
            "action_name": action_name,
            "prompt": f"Confirm action: {action_name}?",
            "created_at": time.time(),
        }
        return apply_mute_state(device_id, base_response(
            True,
            text,
            f"Confirm action: {action_name}?",
            "success",
            command="script_action",
            state={"awaiting": "confirmation", "action": action_name},
        ))

    return script_action_response(text, device_id, action_name)


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
    Command("list_timers", ("list timers", "show timers", "timers", "active timers"), "Show active timers.", handle_list_timers),
    Command("cancel_timer", ("cancel timer", "cancel timers", "stop timer", "stop timers"), "Cancel an active timer.", handle_cancel_timer),
    Command("cancel", ("cancel", "stop", "nevermind", "never mind"), "Cancel a pending command.", handle_cancel),
    Command("repeat", ("repeat", "say"), "Display the spoken suffix.", handle_repeat),
    Command("timer", ("timer", "set timer", "set a timer", "start timer", "start a timer"), "Set a timer.", handle_timer),
    Command("script_action", ("run action", "action", "execute action", "run script"), "Run an allowlisted server script.", handle_script_action),
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
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      color-scheme: dark;
      --bg: #0a0e15;
      --panel: #0f1620;
      --raised: #16202e;
      --inset: #0c111a;
      --text: #d7e1ee;
      --bright: #f2f7fc;
      --muted: #76879c;
      --line: #1d2939;
      --line-bright: #2c3d55;
      --accent: #41d6c5;
      --accent-soft: rgba(65, 214, 197, 0.12);
      --accent-glow: rgba(65, 214, 197, 0.35);
      --good: #4ade83;
      --bad: #ff6b5e;
      --warn: #ffc857;
      --display: "Chakra Petch", "Segoe UI", sans-serif;
      --mono: "IBM Plex Mono", ui-monospace, Consolas, monospace;
      --body: "IBM Plex Sans", system-ui, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    html { scrollbar-color: var(--line-bright) var(--bg); }
    body {
      margin: 0;
      color: var(--text);
      font: 14px/1.45 var(--body);
      background:
        radial-gradient(1100px 520px at 85% -10%, rgba(65, 214, 197, 0.07), transparent 60%),
        radial-gradient(900px 480px at -10% 110%, rgba(65, 214, 197, 0.04), transparent 55%),
        repeating-linear-gradient(0deg, rgba(65, 214, 197, 0.022) 0 1px, transparent 1px 28px),
        repeating-linear-gradient(90deg, rgba(65, 214, 197, 0.022) 0 1px, transparent 1px 28px),
        var(--bg);
      background-attachment: fixed;
    }
    ::selection { background: var(--accent); color: #06251f; }
    @keyframes deck-rise {
      from { opacity: 0; transform: translateY(10px); }
      to { opacity: 1; transform: translateY(0); }
    }
    @keyframes deck-pulse {
      0%, 100% { box-shadow: 0 0 0 0 var(--accent-glow); }
      50% { box-shadow: 0 0 8px 2px var(--accent-glow); }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { animation: none !important; transition: none !important; }
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 16px 22px;
      background: linear-gradient(180deg, rgba(15, 22, 32, 0.96), rgba(15, 22, 32, 0.88));
      backdrop-filter: blur(6px);
      border-bottom: 1px solid var(--line);
      box-shadow: 0 1px 0 rgba(65, 214, 197, 0.14), 0 12px 30px rgba(2, 6, 12, 0.45);
      position: sticky;
      top: 0;
      z-index: 1;
      animation: deck-rise 0.4s ease-out backwards;
    }
    h1 {
      margin: 0;
      font-family: var(--display);
      font-size: 19px;
      font-weight: 700;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--bright);
    }
    h1::before {
      content: "";
      display: inline-block;
      width: 9px;
      height: 16px;
      margin-right: 12px;
      background: var(--accent);
      clip-path: polygon(0 0, 100% 0, 100% 70%, 0 100%);
      box-shadow: 0 0 12px var(--accent-glow);
      vertical-align: -2px;
    }
    main {
      width: min(1880px, 100%);
      margin: 0 auto;
      padding: 20px clamp(16px, 2.4vw, 36px);
      animation: deck-rise 0.5s ease-out 0.1s backwards;
    }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 12px;
      color: var(--muted);
      flex-wrap: wrap;
      font-family: var(--mono);
      font-size: 12.5px;
    }
    .tabs {
      display: flex;
      gap: 2px;
      padding: 10px clamp(16px, 2.4vw, 36px) 0;
      margin: 0 auto;
      width: min(1880px, 100%);
      overflow-x: auto;
      animation: deck-rise 0.4s ease-out 0.05s backwards;
    }
    .tab-button {
      white-space: nowrap;
      background: transparent;
      border: 0;
      border-bottom: 2px solid transparent;
      border-radius: 0;
      height: 36px;
      padding: 0 14px;
      font-family: var(--display);
      font-size: 12.5px;
      font-weight: 600;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .tab-button:hover {
      color: var(--text);
      border-color: var(--line-bright);
    }
    .tab-button.active {
      color: var(--accent);
      border-bottom-color: var(--accent);
      background: linear-gradient(180deg, transparent 55%, var(--accent-soft));
      text-shadow: 0 0 14px var(--accent-glow);
    }
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }
    button, input, select, textarea {
      border: 1px solid var(--line-bright);
      background: var(--raised);
      color: var(--text);
      padding: 0 12px;
      border-radius: 3px;
      font-family: var(--body);
      font-size: 13.5px;
    }
    button, input, select { height: 34px; }
    textarea {
      min-height: 76px;
      padding: 8px 10px;
      resize: vertical;
    }
    button {
      cursor: pointer;
      font-family: var(--display);
      font-weight: 600;
      letter-spacing: 0.06em;
      transition: border-color 0.15s, box-shadow 0.15s, color 0.15s;
    }
    button.danger { border-color: var(--bad); color: var(--bad); }
    button.danger:hover { box-shadow: 0 0 10px rgba(255, 107, 94, 0.35); }
    input, select, textarea { min-width: 0; }
    input::placeholder, textarea::placeholder { color: var(--muted); opacity: 0.7; }
    input:focus-visible, select:focus-visible, textarea:focus-visible, button:focus-visible {
      outline: 1px solid var(--accent);
      outline-offset: 1px;
    }
    button:hover {
      border-color: var(--accent);
      color: var(--accent);
      box-shadow: 0 0 10px var(--accent-soft);
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .stat, .panel {
      background: linear-gradient(180deg, var(--raised) 0%, var(--panel) 70%);
      border: 1px solid var(--line);
      border-radius: 4px;
    }
    .stat {
      padding: 13px 14px;
      min-height: 82px;
      clip-path: polygon(0 0, calc(100% - 14px) 0, 100% 14px, 100% 100%, 0 100%);
      border-top: 1px solid var(--line-bright);
      position: relative;
      animation: deck-rise 0.45s ease-out backwards;
    }
    .stat:nth-child(2) { animation-delay: 0.05s; }
    .stat:nth-child(3) { animation-delay: 0.1s; }
    .stat:nth-child(4) { animation-delay: 0.15s; }
    .stat:nth-child(5) { animation-delay: 0.2s; }
    .stat:nth-child(6) { animation-delay: 0.25s; }
    .stat:nth-child(7) { animation-delay: 0.3s; }
    .stat:nth-child(8) { animation-delay: 0.35s; }
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
    .stat .label {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 10.5px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }
    .stat .value {
      display: block;
      margin-top: 6px;
      font-family: var(--display);
      font-size: 27px;
      font-weight: 700;
      color: var(--bright);
      text-shadow: 0 0 18px var(--accent-soft);
    }
    .grid {
      display: grid;
      grid-template-columns: minmax(900px, 1fr) minmax(360px, 440px);
      gap: 22px;
      align-items: start;
    }
    .overview-grid {
      display: grid;
      grid-template-columns: minmax(420px, 1.2fr) minmax(360px, 0.8fr);
      gap: 22px;
      align-items: start;
    }
    .overview-system {
      align-items: stretch;
      margin-bottom: 18px;
    }
    .overview-system > .panel { height: 100%; }
    .panel-stack {
      display: grid;
      gap: 18px;
      min-width: 0;
    }
    .grid > .panel { min-width: 0; }
    .grid > div { min-width: 0; }
    .panel { overflow: hidden; }
    .panel h2 {
      margin: 0;
      padding: 13px 16px;
      border-bottom: 1px solid var(--line);
      font-family: var(--display);
      font-size: 13px;
      font-weight: 600;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--accent);
    }
    .panel h2::before { content: "// "; color: var(--line-bright); }
    .panel-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 9px 12px 9px 16px;
      border-bottom: 1px solid var(--line);
    }
    .panel-header h2 { padding: 0; border-bottom: 0; }
    .collapse-button {
      min-width: 30px;
      width: 30px;
      height: 28px;
      padding: 0;
      font-weight: 700;
      line-height: 1;
      font-family: var(--mono);
    }
    .panel.collapsed .collapsible-content { display: none; }
    table { width: 100%; border-collapse: collapse; }
    th, td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 10.5px;
      font-weight: 600;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }
    tr:last-child td { border-bottom: 0; }
    tbody tr:hover td { background: rgba(65, 214, 197, 0.035); }
    .device-id {
      font-weight: 600;
      word-break: break-word;
      color: var(--bright);
    }
    .meta {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11.5px;
      margin-top: 3px;
    }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      white-space: nowrap;
      font-family: var(--mono);
      font-size: 12px;
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--bad);
      box-shadow: 0 0 7px rgba(255, 107, 94, 0.6);
    }
    .online .dot {
      background: var(--good);
      box-shadow: 0 0 7px rgba(74, 222, 131, 0.7);
      animation: deck-pulse 2.4s ease-in-out infinite;
    }
    .chips { display: flex; gap: 6px; flex-wrap: wrap; }
    .chip {
      border: 1px solid var(--line-bright);
      border-radius: 2px;
      padding: 2px 8px;
      color: var(--muted);
      background: var(--inset);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.03em;
      white-space: nowrap;
    }
    .firmware { display: grid; gap: 3px; }
    .firmware-version { font-weight: 600; font-family: var(--mono); font-size: 12.5px; }
    .links { display: flex; gap: 8px; flex-wrap: wrap; }
    .name-form {
      display: flex;
      gap: 6px;
      margin-top: 8px;
      max-width: 320px;
    }
    .name-form input { flex: 1; width: 100%; }
    .name-form button { flex: 0 0 auto; }
    .action-form { display: grid; gap: 8px; margin-top: 8px; }
    .action-form input, .action-form select, .action-form textarea { width: 100%; }
    .action-row {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    .mission-layout {
      display: grid;
      grid-template-columns: minmax(0, 1.25fr) minmax(280px, 0.75fr);
      gap: 12px;
      padding: 12px;
      border-bottom: 1px solid var(--line);
    }
    .calendar-card {
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 3px;
      background: var(--panel);
    }
    .calendar-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 10px;
    }
    .calendar-title {
      font-family: var(--display);
      font-size: 13px;
      font-weight: 600;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--bright);
    }
    .calendar-controls { display: flex; gap: 6px; flex-wrap: wrap; }
    .calendar-controls button { height: 30px; padding: 0 10px; }
    .calendar-weekdays,
    .calendar-grid {
      display: grid;
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 4px;
    }
    .calendar-weekdays {
      margin-bottom: 4px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 10.5px;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      text-align: center;
    }
    .calendar-day {
      min-height: 88px;
      min-width: 0;
      padding: 7px;
      border: 1px solid var(--line);
      border-radius: 3px;
      background: var(--inset);
    }
    .calendar-day.outside { opacity: 0.45; }
    .calendar-day.today { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent-soft); }
    .calendar-date {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11.5px;
      margin-bottom: 5px;
    }
    .calendar-day.today .calendar-date { color: var(--accent); }
    .calendar-task {
      display: block;
      width: 100%;
      height: auto;
      margin-top: 4px;
      padding: 4px 5px;
      border: 1px solid rgba(65, 214, 197, 0.28);
      border-radius: 2px;
      background: var(--accent-soft);
      color: var(--bright);
      font-family: var(--mono);
      font-size: 10.5px;
      line-height: 1.25;
      overflow: hidden;
      text-align: left;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .calendar-more {
      margin-top: 4px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 10.5px;
    }
    .persistent-lane {
      display: grid;
      gap: 8px;
      align-content: start;
    }
    .filter-bar {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) repeat(3, minmax(150px, 200px));
      gap: 10px;
      padding: 12px;
      border-bottom: 1px solid var(--line);
      background: var(--inset);
    }
    .filter-meta {
      padding: 8px 12px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11.5px;
      border-bottom: 1px solid var(--line);
    }
    .device-list { display: grid; gap: 10px; padding: 12px; }
    .device-row {
      border: 1px solid var(--line);
      border-radius: 3px;
      overflow: hidden;
      background: var(--panel);
      transition: border-color 0.15s;
    }
    .device-row:hover { border-color: var(--line-bright); }
    .device-row.expanded { border-color: var(--accent); box-shadow: 0 0 14px var(--accent-soft); }
    .device-summary {
      display: grid;
      grid-template-columns: minmax(220px, 1.4fr) minmax(140px, 0.75fr) minmax(140px, 0.8fr) minmax(150px, 0.9fr) auto;
      gap: 12px;
      align-items: center;
      padding: 12px;
    }
    .device-title { display: grid; gap: 3px; min-width: 0; }
    .device-title .device-id { font-size: 15px; }
    .device-detail {
      display: none;
      border-top: 1px solid var(--line);
      padding: 12px;
      background: var(--inset);
    }
    .device-row.expanded .device-detail { display: block; }
    .detail-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 12px;
    }
    .detail-block {
      border: 1px solid var(--line);
      border-radius: 3px;
      padding: 10px;
      min-width: 0;
      background: var(--panel);
    }
    .detail-title {
      color: var(--accent);
      font-family: var(--mono);
      font-size: 10.5px;
      font-weight: 600;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      margin-bottom: 8px;
    }
    .kv {
      display: grid;
      grid-template-columns: minmax(90px, 0.45fr) minmax(0, 1fr);
      gap: 6px 10px;
      font-family: var(--mono);
      font-size: 12.5px;
    }
    .kv-key { color: var(--muted); }
    .metric-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 12px;
      padding: 12px;
    }
    .metric-card {
      border: 1px solid var(--line);
      border-radius: 3px;
      padding: 12px;
      background: var(--panel);
      min-width: 0;
    }
    .metric-head {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .metric-value {
      margin-top: 7px;
      font-family: var(--display);
      font-size: 24px;
      font-weight: 700;
      color: var(--bright);
    }
    .meter {
      height: 7px;
      margin-top: 9px;
      overflow: hidden;
      border: 1px solid var(--line-bright);
      border-radius: 2px;
      background: var(--inset);
    }
    .meter-fill {
      height: 100%;
      width: 0;
      background: linear-gradient(90deg, var(--accent), var(--good));
      box-shadow: 0 0 10px var(--accent-glow);
    }
    .diagnostics { display: grid; gap: 8px; padding: 12px; }
    .diagnostic {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      border: 1px solid var(--line);
      border-left-width: 3px;
      border-radius: 3px;
      padding: 8px 10px;
      background: var(--panel);
    }
    .diagnostic .device-id { flex-shrink: 0; word-break: normal; }
    .diagnostic.ok { border-left-color: var(--good); }
    .diagnostic.ok .device-id { color: var(--good); }
    .diagnostic:not(.ok) { border-left-color: var(--warn); }
    .diagnostic:not(.ok) .device-id { color: var(--warn); }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; text-shadow: 0 0 12px var(--accent-glow); }
    .events { display: grid; gap: 12px; padding: 12px; }
    .event {
      border: 1px solid var(--line);
      border-radius: 3px;
      padding: 10px;
      background: var(--panel);
    }
    .empty {
      padding: 18px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12.5px;
    }
    .empty::before { content: "[ "; color: var(--line-bright); }
    .empty::after { content: " ]"; color: var(--line-bright); }
    .modal-backdrop {
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 20px;
      background: rgba(4, 8, 14, 0.7);
      backdrop-filter: blur(3px);
      z-index: 10;
    }
    .modal-backdrop.open { display: flex; }
    .modal {
      width: min(420px, 100%);
      background: var(--raised);
      border: 1px solid var(--line-bright);
      border-top: 2px solid var(--bad);
      border-radius: 4px;
      padding: 18px;
      box-shadow: 0 16px 50px rgba(0, 0, 0, 0.6);
      animation: deck-rise 0.25s ease-out;
    }
    .modal.wide { width: min(620px, 100%); border-top-color: var(--accent); }
    .modal-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }
    .modal-header h2 { margin: 0; }
    .icon-button {
      width: 32px;
      height: 32px;
      padding: 0;
      font-family: var(--mono);
      font-size: 15px;
      line-height: 1;
    }
    .modal h2 {
      margin: 0 0 8px;
      font-family: var(--display);
      font-size: 15px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .modal-actions {
      display: flex;
      justify-content: flex-end;
      gap: 10px;
      margin-top: 18px;
    }
    code {
      font-family: var(--mono);
      font-size: 12px;
      word-break: break-word;
      color: var(--accent);
    }
    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--line-bright); border-radius: 5px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--accent); }
    @media (max-width: 900px) {
      header { align-items: flex-start; flex-direction: column; }
      .grid, .overview-grid, .mission-layout { grid-template-columns: 1fr; }
      .calendar-day { min-height: 68px; padding: 5px; }
      .filter-bar { grid-template-columns: 1fr; }
      .device-summary { grid-template-columns: 1fr; }
      table, thead, tbody, th, td, tr { display: block; }
      thead { display: none; }
      tr { border-bottom: 1px solid var(--line); padding: 10px 0; }
      td { border-bottom: 0; padding: 6px 12px; }
      td::before {
        content: attr(data-label);
        display: block;
        color: var(--muted);
        font-family: var(--mono);
        font-size: 10.5px;
        letter-spacing: 0.1em;
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
      <button id="restartButton" type="button" disabled>Restart</button>
    </div>
  </header>
  <nav class="tabs" aria-label="Dashboard sections">
    <button class="tab-button active" type="button" data-tab="overview">Overview</button>
    <button class="tab-button" type="button" data-tab="mission">Mission</button>
    <button class="tab-button" type="button" data-tab="devices">Devices</button>
    <button class="tab-button" type="button" data-tab="events">Events</button>
    <button class="tab-button" type="button" data-tab="uptime">Uptime</button>
    <button class="tab-button" type="button" data-tab="rules">Rules</button>
    <button class="tab-button" type="button" data-tab="actions">Actions</button>
    <button class="tab-button" type="button" data-tab="firmware">Firmware</button>
  </nav>
  <main>
    <section class="tab-panel active" data-tab-panel="overview">
      <section class="stats" id="stats"></section>
      <section class="grid overview-system">
        <div class="panel">
          <h2>Host Resources</h2>
          <div class="metric-grid" id="serverMetrics"></div>
        </div>
        <div class="panel-stack">
          <div class="panel">
            <h2>Host Details</h2>
            <div class="events" id="serverDetails"></div>
          </div>
          <div class="panel" id="serverGpuPanel">
            <h2>GPU</h2>
            <div class="events" id="serverGpu"></div>
          </div>
        </div>
      </section>
      <section class="overview-grid">
        <div class="panel-stack">
          <div class="panel collapsible-panel" data-panel-id="startup">
            <div class="panel-header">
              <h2>Startup</h2>
              <button class="collapse-button" type="button" aria-label="Toggle Startup" aria-expanded="true">-</button>
            </div>
            <div class="diagnostics collapsible-content" id="diagnostics"></div>
          </div>
          <div class="panel">
            <h2>Timers</h2>
            <div class="events">
              <div class="event">
                <div class="device-id">Create timer</div>
                <form class="action-form" id="timerForm">
                  <input id="timerName" type="text" placeholder="Name, optional" autocomplete="off">
                  <input id="timerDuration" type="text" placeholder="Duration, e.g. 5 minutes" autocomplete="off">
                  <select id="timerMode">
                    <option value="device">Alert selected device</option>
                    <option value="all_devices">Alert all devices</option>
                    <option value="phone">Phone notification</option>
                  </select>
                  <select id="timerDevice"></select>
                  <div class="action-row">
                    <button type="submit">Start</button>
                    <span class="meta" id="timerFormResult"></span>
                  </div>
                </form>
              </div>
            </div>
            <div class="events" id="timers"></div>
          </div>
          <div class="panel">
            <h2>r1-note</h2>
            <div class="events">
              <form class="action-form" id="r1NoteForm">
                <textarea id="r1NoteText" placeholder="Note text"></textarea>
                <div class="action-row">
                  <button type="submit">Save Note</button>
                  <span class="meta" id="r1NoteMeta"></span>
                </div>
              </form>
            </div>
          </div>
        </div>
        <div class="panel-stack">
          <div class="panel collapsible-panel" data-panel-id="attention">
            <div class="panel-header">
              <h2>Attention</h2>
              <button class="collapse-button" type="button" aria-label="Toggle Attention" aria-expanded="true">-</button>
            </div>
            <div class="events collapsible-content" id="attention"></div>
          </div>
          <div class="panel collapsible-panel" data-panel-id="recentActivity">
            <div class="panel-header">
              <h2>Recent Activity</h2>
              <button class="collapse-button" type="button" aria-label="Toggle Recent Activity" aria-expanded="true">-</button>
            </div>
            <div class="events collapsible-content" id="activity"></div>
          </div>
        </div>
      </section>
    </section>
    <section class="tab-panel" data-tab-panel="mission">
      <div class="panel">
        <div class="panel-header">
          <h2>Mission Board</h2>
          <div class="action-row">
            <span class="meta" id="missionCount"></span>
            <button id="openMissionForm" type="button">Add Task</button>
          </div>
        </div>
        <div class="mission-layout">
          <section class="calendar-card" aria-label="Mission calendar">
            <div class="calendar-head">
              <div class="calendar-title" id="missionCalendarTitle">Calendar</div>
              <div class="calendar-controls">
                <button id="missionCalendarPrev" type="button">Prev</button>
                <button id="missionCalendarToday" type="button">Today</button>
                <button id="missionCalendarNext" type="button">Next</button>
              </div>
            </div>
            <div class="calendar-weekdays" aria-hidden="true">
              <div>Mon</div><div>Tue</div><div>Wed</div><div>Thu</div><div>Fri</div><div>Sat</div><div>Sun</div>
            </div>
            <div class="calendar-grid" id="missionCalendar"></div>
          </section>
          <section class="persistent-lane">
            <div class="device-id">Persistent</div>
            <div class="meta" id="persistentMissionCount"></div>
            <div class="events" id="persistentMissionTasks"></div>
          </section>
        </div>
        <div class="filter-meta">Open Tasks <span id="activeMissionCount"></span></div>
        <div class="events" id="missionTasks"></div>
      </div>
    </section>
    <section class="tab-panel" data-tab-panel="devices">
      <div class="panel">
        <h2>Devices</h2>
        <div class="filter-bar">
          <input id="deviceSearch" type="search" placeholder="Search devices" autocomplete="off">
          <select id="deviceStatusFilter">
            <option value="">All statuses</option>
            <option value="online">Online</option>
            <option value="offline">Offline</option>
          </select>
          <select id="deviceTypeFilter">
            <option value="">All types</option>
          </select>
          <select id="deviceCapabilityFilter">
            <option value="">All capabilities</option>
          </select>
        </div>
        <div class="filter-meta" id="deviceFilterMeta">Showing all devices</div>
        <div id="devices"></div>
      </div>
    </section>
    <section class="tab-panel" data-tab-panel="actions">
      <section class="grid">
        <div class="panel collapsible-panel" data-panel-id="actions">
          <div class="panel-header">
            <h2>Actions</h2>
            <button class="collapse-button" type="button" aria-label="Toggle Actions" aria-expanded="true">-</button>
          </div>
          <div class="events collapsible-content" id="actions"></div>
        </div>
        <div class="panel">
          <h2>Simulate Transcript</h2>
          <div class="events">
            <div class="event">
              <div class="action-form command-form">
                <input id="simulateTranscript" type="text" value="status" autocomplete="off">
                <input id="simulateDeviceId" type="text" value="dashboard" autocomplete="off">
                <div class="action-row">
                  <button id="simulateButton" type="button">Run</button>
                  <span class="meta" id="simulateResult"></span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>
    </section>
    <section class="tab-panel" data-tab-panel="rules">
      <div class="panel collapsible-panel" data-panel-id="eventRules">
        <div class="panel-header">
          <h2>Event Rules</h2>
          <button class="collapse-button" type="button" aria-label="Toggle Event Rules" aria-expanded="true">-</button>
        </div>
        <div class="events collapsible-content" id="rules"></div>
      </div>
    </section>
    <section class="tab-panel" data-tab-panel="events">
      <section class="grid">
        <div class="panel collapsible-panel" data-panel-id="recentCommands">
          <div class="panel-header">
            <h2>Recent Commands</h2>
            <button class="collapse-button" type="button" aria-label="Toggle Recent Commands" aria-expanded="true">-</button>
          </div>
          <div class="events collapsible-content" id="commands"></div>
        </div>
        <div class="panel">
          <h2>Button Events</h2>
          <div class="events" id="buttonEvents"></div>
        </div>
      </section>
    </section>
    <section class="tab-panel" data-tab-panel="uptime">
      <div class="panel">
        <div class="panel-header">
          <h2>Uptime Tracker</h2>
          <div class="action-row">
            <button id="openUptimeForm" type="button">Register</button>
          </div>
        </div>
        <div class="events" id="uptimeMonitors"></div>
      </div>
    </section>
    <section class="tab-panel" data-tab-panel="firmware">
      <div class="panel">
        <h2>Firmware Catalog</h2>
        <div class="events" id="firmwareCatalog"></div>
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
  <div class="modal-backdrop" id="missionModal" role="dialog" aria-modal="true" aria-labelledby="missionModalTitle">
    <div class="modal wide">
      <div class="modal-header">
        <h2 id="missionModalTitle">Add Task</h2>
        <button id="closeMissionForm" class="icon-button" type="button" aria-label="Close add task modal">x</button>
      </div>
      <form class="action-form" id="missionForm">
        <input id="missionTitle" type="text" placeholder="Task title" autocomplete="off">
        <select id="missionType">
          <option value="persistent">Persistent</option>
          <option value="daily">Dated</option>
        </select>
        <input id="missionDueDate" type="date">
        <textarea id="missionNotes" placeholder="Notes, optional"></textarea>
        <div class="action-row">
          <button type="submit">Create Task</button>
          <button id="cancelMissionForm" type="button">Cancel</button>
          <span class="meta" id="missionFormResult"></span>
        </div>
      </form>
    </div>
  </div>
  <div class="modal-backdrop" id="uptimeModal" role="dialog" aria-modal="true" aria-labelledby="uptimeModalTitle">
    <div class="modal wide">
      <div class="modal-header">
        <h2 id="uptimeModalTitle">Register Monitor</h2>
        <button id="closeUptimeForm" class="icon-button" type="button" aria-label="Close uptime monitor modal">x</button>
      </div>
      <form class="action-form" id="uptimeForm">
        <input id="uptimeId" type="hidden">
        <input id="uptimeName" type="text" placeholder="Name, optional" autocomplete="off">
        <input id="uptimeTarget" type="text" placeholder="IP, hostname, or https://example.com" autocomplete="off">
        <input id="uptimeInterval" type="number" min="30" step="30" value="600" autocomplete="off">
        <div class="action-row">
          <button id="uptimeSubmitButton" type="submit">Register</button>
          <button id="uptimeCancelEditButton" type="button">Cancel Edit</button>
          <button id="cancelUptimeForm" type="button">Cancel</button>
          <span class="meta" id="uptimeFormResult"></span>
        </div>
      </form>
    </div>
  </div>
  <script>
    const state = {
      refreshMs: 5000,
      timer: null,
      pendingRemoval: null,
      devices: [],
      expandedDevices: new Set(),
    };
    let missionCalendarDate = new Date();
    missionCalendarDate.setDate(1);
    let latestMissionToday = "";

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

    function setPanelCollapsed(panel, collapsed) {
      panel.classList.toggle("collapsed", collapsed);
      const button = panel.querySelector(".collapse-button");
      if (button) {
        button.textContent = collapsed ? "+" : "-";
        button.setAttribute("aria-expanded", String(!collapsed));
      }
      localStorage.setItem(`dashboard-panel-${panel.dataset.panelId}`, collapsed ? "1" : "0");
    }

    function initCollapsiblePanels() {
      for (const panel of document.querySelectorAll(".collapsible-panel")) {
        const saved = localStorage.getItem(`dashboard-panel-${panel.dataset.panelId}`);
        setPanelCollapsed(panel, saved === "1");
        const button = panel.querySelector(".collapse-button");
        if (button) {
          button.addEventListener("click", () => setPanelCollapsed(panel, !panel.classList.contains("collapsed")));
        }
      }
    }

    function setActiveTab(tabName) {
      for (const button of document.querySelectorAll(".tab-button")) {
        button.classList.toggle("active", button.dataset.tab === tabName);
      }
      for (const panel of document.querySelectorAll(".tab-panel")) {
        panel.classList.toggle("active", panel.dataset.tabPanel === tabName);
      }
      localStorage.setItem("dashboard-active-tab", tabName);
    }

    function initTabs() {
      for (const button of document.querySelectorAll(".tab-button")) {
        button.addEventListener("click", () => setActiveTab(button.dataset.tab));
      }
      const saved = localStorage.getItem("dashboard-active-tab") || "overview";
      const exists = document.querySelector(`.tab-button[data-tab="${saved}"]`);
      setActiveTab(exists ? saved : "overview");
    }

    function initDeviceFilters() {
      const saved = loadDeviceFilters();
      document.getElementById("deviceSearch").value = saved.query || "";
      document.getElementById("deviceStatusFilter").value = saved.status || "";
      document.getElementById("deviceTypeFilter").value = saved.type || "";
      document.getElementById("deviceCapabilityFilter").value = saved.capability || "";
      for (const id of ["deviceSearch", "deviceStatusFilter", "deviceTypeFilter", "deviceCapabilityFilter"]) {
        document.getElementById(id).addEventListener("input", () => {
          saveDeviceFilters();
          renderFilteredDevices();
        });
        document.getElementById(id).addEventListener("change", () => {
          saveDeviceFilters();
          renderFilteredDevices();
        });
      }
    }

    function renderStats(data) {
      const stats = document.getElementById("stats");
      const items = [
        ["Devices", data.summary.device_count],
        ["Online", data.summary.online_count],
        ["Offline", data.summary.offline_count],
        ["Pending Events", data.summary.pending_event_count],
        ["Timers", data.summary.active_timer_count],
        ["Rules", data.summary.rule_count],
        ["Commands", data.summary.recent_command_count],
        ["Buttons", data.summary.recent_button_event_count],
        ["Firmware", (data.firmware_catalog || []).length],
      ];
      const existingCards = Array.from(stats.children);
      for (const [index, [label, value]] of items.entries()) {
        const displayedValue = String(value);
        const existingCard = existingCards[index];
        if (existingCard?.dataset.label === label && existingCard.dataset.value === displayedValue) {
          continue;
        }
        const card = el("div", "stat");
        card.dataset.label = label;
        card.dataset.value = displayedValue;
        card.append(el("div", "label", label));
        card.append(el("span", "value", value));
        if (existingCard) {
          existingCard.replaceWith(card);
        } else {
          stats.append(card);
        }
      }
      for (const extraCard of existingCards.slice(items.length)) {
        extraCard.remove();
      }
    }

    function batteryPercent(device) {
      const status = device.status || {};
      for (const key of ["battery_percent", "battery_pct", "battery_level", "battery"]) {
        const value = status[key];
        if (Number.isFinite(value)) return value > 1 ? value : value * 100;
      }
      return null;
    }

    function renderAttention(devices) {
      const root = document.getElementById("attention");
      root.replaceChildren();
      const items = [];
      for (const device of devices || []) {
        const battery = batteryPercent(device);
        if (!device.online) {
          items.push({
            level: "offline",
            title: `${device.display_name || device.id} offline`,
            detail: `${device.id} | ${text(device.online_detail)} | ${age(device.age_seconds)}`,
          });
        } else if (battery !== null && battery <= 20) {
          items.push({
            level: "battery",
            title: `${device.display_name || device.id} low battery`,
            detail: `${Math.round(battery)}% | ${device.id}`,
          });
        } else if (device.pending_events) {
          items.push({
            level: "events",
            title: `${device.display_name || device.id} has queued events`,
            detail: `${device.pending_events} event(s) queued | ${device.id}`,
          });
        }
      }
      if (!items.length) {
        root.append(el("div", "empty", "No device issues need attention."));
        return;
      }
      for (const item of items.slice(0, 8)) {
        const row = el("div", "event");
        row.append(el("div", "device-id", item.title));
        row.append(el("div", "meta", item.detail));
        root.append(row);
      }
    }

    function renderActivity(data) {
      const root = document.getElementById("activity");
      root.replaceChildren();
      const items = [];
      for (const command of data.recent_commands || []) {
        items.push({
          at: command.received_at || 0,
          title: `Command: ${text(command.command)}`,
          detail: `${text(command.device_id)} | heard: ${text(command.text)} | ${text(command.display_text)}`,
        });
      }
      for (const event of data.recent_button_events || []) {
        items.push({
          at: event.received_at || 0,
          title: `Button: ${text(event.button)}`,
          detail: `${text(event.device_id)} | gpio ${text(event.gpio)} | count ${text(event.click_count)}`,
        });
      }
      for (const run of data.recent_rule_runs || []) {
        items.push({
          at: run.received_at || 0,
          title: `Rule: ${text(run.rule_id)}`,
          detail: `${run.ok ? "ok" : "failed"} | ${text(run.device_id)} | ${text(run.result)}`,
        });
      }
      items.sort((a, b) => b.at - a.at);
      if (!items.length) {
        root.append(el("div", "empty", "No recent activity."));
        return;
      }
      for (const item of items.slice(0, 10)) {
        const row = el("div", "event");
        row.append(el("div", "device-id", item.title));
        row.append(el("div", "meta", item.at ? new Date(item.at * 1000).toLocaleTimeString() : "-"));
        row.append(el("div", "", item.detail));
        root.append(row);
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

    function limitedChips(items, limit = 4) {
      const visible = (items || []).slice(0, limit);
      const wrap = chips(visible);
      const remaining = (items || []).length - visible.length;
      if (remaining > 0) wrap.append(el("span", "chip", `+${remaining}`));
      return wrap;
    }

    function firmwareDetails(device) {
      const firmware = device.firmware || {};
      const project = device.firmware_project || firmware.project || "";
      const version = device.firmware_version || firmware.version || "";
      const target = firmware.target || device.status?.target || "";
      const deviceType = device.device_type || firmware.device_type || "";
      const hasFirmware = Boolean(project || version || target || deviceType);
      const wrap = el("div", "firmware");

      if (!hasFirmware) {
        wrap.append(el("span", "meta", "Not reported"));
        return wrap;
      }

      wrap.append(el("div", "firmware-version", version || "Unknown version"));
      if (project) wrap.append(el("div", "meta", project));
      if (target) wrap.append(el("div", "meta", `target: ${target}`));
      if (deviceType) wrap.append(el("div", "meta", `type: ${deviceType}`));
      return wrap;
    }

    function firmwareSummary(device) {
      const firmware = device.firmware || {};
      return device.firmware_version || firmware.version || device.firmware_project || firmware.project || "No firmware";
    }

    function deviceSearchText(device) {
      const fields = [
        device.id,
        device.friendly_name,
        device.display_name,
        device.ip,
        device.remote_addr,
        device.type,
        device.model,
        device.device_type,
        device.firmware_version,
        device.firmware_project,
        ...(device.capabilities || []),
        ...Object.values(device.status || {}),
      ];
      return fields.map((value) => text(value).toLowerCase()).join(" ");
    }

    function selectedDeviceFilters() {
      return {
        query: document.getElementById("deviceSearch")?.value.trim().toLowerCase() || "",
        status: document.getElementById("deviceStatusFilter")?.value || "",
        type: document.getElementById("deviceTypeFilter")?.value || "",
        capability: document.getElementById("deviceCapabilityFilter")?.value || "",
      };
    }

    function saveDeviceFilters() {
      localStorage.setItem("dashboard-device-filters", JSON.stringify(selectedDeviceFilters()));
    }

    function loadDeviceFilters() {
      try {
        return JSON.parse(localStorage.getItem("dashboard-device-filters") || "{}");
      } catch (_error) {
        return {};
      }
    }

    function deviceMatchesFilters(device, filters) {
      if (filters.query && !deviceSearchText(device).includes(filters.query)) return false;
      if (filters.status === "online" && !device.online) return false;
      if (filters.status === "offline" && device.online) return false;
      if (filters.type && device.type !== filters.type) return false;
      if (filters.capability && !(device.capabilities || []).includes(filters.capability)) return false;
      return true;
    }

    function populateDeviceFilterOptions(devices) {
      const saved = loadDeviceFilters();
      const typeSelect = document.getElementById("deviceTypeFilter");
      const capabilitySelect = document.getElementById("deviceCapabilityFilter");
      const currentType = typeSelect.value || saved.type || "";
      const currentCapability = capabilitySelect.value || saved.capability || "";
      const types = [...new Set((devices || []).map((device) => device.type).filter(Boolean))].sort();
      const capabilities = [...new Set((devices || []).flatMap((device) => device.capabilities || []))].sort();

      typeSelect.replaceChildren(new Option("All types", ""));
      for (const type of types) typeSelect.append(new Option(type, type));
      typeSelect.value = types.includes(currentType) ? currentType : "";

      capabilitySelect.replaceChildren(new Option("All capabilities", ""));
      for (const capability of capabilities) capabilitySelect.append(new Option(capability, capability));
      capabilitySelect.value = capabilities.includes(currentCapability) ? currentCapability : "";
    }

    function filteredDevices(devices) {
      const filters = selectedDeviceFilters();
      return (devices || []).filter((device) => deviceMatchesFilters(device, filters));
    }

    function renderDeviceFilterMeta(filtered, total) {
      const root = document.getElementById("deviceFilterMeta");
      const filters = selectedDeviceFilters();
      const active = [];
      if (filters.query) active.push(`search "${filters.query}"`);
      if (filters.status) active.push(filters.status);
      if (filters.type) active.push(filters.type);
      if (filters.capability) active.push(filters.capability);
      const suffix = active.length ? ` | ${active.join(", ")}` : "";
      root.textContent = `Showing ${filtered.length} of ${total} device${total === 1 ? "" : "s"}${suffix}`;
    }

    function renderFilteredDevices() {
      const devices = filteredDevices(state.devices);
      renderDeviceFilterMeta(devices, state.devices.length);
      renderDevices(devices);
    }

    function toggleDeviceDetails(deviceId) {
      if (state.expandedDevices.has(deviceId)) {
        state.expandedDevices.delete(deviceId);
      } else {
        state.expandedDevices.add(deviceId);
      }
      localStorage.setItem("dashboard-expanded-devices", JSON.stringify([...state.expandedDevices]));
      renderFilteredDevices();
    }

    function initExpandedDevices() {
      try {
        state.expandedDevices = new Set(JSON.parse(localStorage.getItem("dashboard-expanded-devices") || "[]"));
      } catch (_error) {
        state.expandedDevices = new Set();
      }
    }

    function keyValueRows(entries) {
      const wrap = el("div", "kv");
      for (const [key, value] of entries) {
        wrap.append(el("div", "kv-key", key));
        wrap.append(el("div", "", text(value)));
      }
      return wrap;
    }

    function objectRows(object) {
      return Object.entries(object || {}).map(([key, value]) => [key, typeof value === "object" ? JSON.stringify(value) : value]);
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
      const list = el("div", "device-list");
      for (const device of devices) {
        const row = el("div", `device-row ${state.expandedDevices.has(device.id) ? "expanded" : ""}`);
        const summary = el("div", "device-summary");
        const title = el("div", "device-title");
        title.append(el("div", "device-id", text(device.display_name || device.id)));
        title.append(el("div", "meta", `${text(device.id)} | ${text(device.ip)}`));

        const statusBlock = el("div");
        const status = el("span", `status ${device.online ? "online" : ""}`);
        status.append(el("span", "dot"));
        status.append(el("span", "", device.online ? "Online" : "Offline"));
        statusBlock.append(status);
        statusBlock.append(el("div", "meta", `${text(device.online_detail)} | ${age(device.age_seconds)}`));
        if (device.muted) statusBlock.append(el("div", "meta", "Muted"));
        if (device.pending_events) statusBlock.append(el("div", "meta", `${device.pending_events} event(s) queued`));

        const typeBlock = el("div");
        typeBlock.append(el("div", "device-id", text(device.type)));
        typeBlock.append(el("div", "meta", text(device.model)));

        const firmwareBlock = el("div");
        firmwareBlock.append(el("div", "device-id", firmwareSummary(device)));

        const actions = el("div", "action-row");
        const detailsButton = el("button", "", state.expandedDevices.has(device.id) ? "Hide Details" : "Details");
        detailsButton.type = "button";
        detailsButton.addEventListener("click", () => toggleDeviceDetails(device.id));
        const removeButton = el("button", "danger", "Remove");
        removeButton.type = "button";
        removeButton.addEventListener("click", () => openRemoveModal(device));
        actions.append(detailsButton, removeButton);

        summary.append(title, statusBlock, typeBlock, firmwareBlock, actions);

        const detail = el("div", "device-detail");
        const grid = el("div", "detail-grid");
        const identity = el("div", "detail-block");
        identity.append(el("div", "detail-title", "Identity"));
        identity.append(friendlyNameForm(device));
        identity.append(keyValueRows([
          ["Device ID", device.id],
          ["Display name", device.display_name],
          ["Type", device.type],
          ["Model", device.model],
          ["IP", device.ip],
          ["Remote addr", device.remote_addr],
        ]));

        const stateBlock = el("div", "detail-block");
        stateBlock.append(el("div", "detail-title", "State"));
        stateBlock.append(keyValueRows([
          ["Online", device.online ? "yes" : "no"],
          ["Detail", device.online_detail],
          ["Seen via", device.last_seen_via || ""],
          ["Last seen", age(device.age_seconds)],
          ["Last local", device.last_local_seen ? new Date(device.last_local_seen * 1000).toLocaleString() : ""],
          ["Last relay", device.last_relay_seen ? new Date(device.last_relay_seen * 1000).toLocaleString() : ""],
          ["Requests", device.request_count],
          ["Pending events", device.pending_events],
          ["Muted", device.muted ? "yes" : "no"],
        ]));

        const firmware = el("div", "detail-block");
        firmware.append(el("div", "detail-title", "Firmware"));
        firmware.append(firmwareDetails(device));

        const caps = el("div", "detail-block");
        caps.append(el("div", "detail-title", "Capabilities"));
        caps.append(chips(device.capabilities));

        const endpoints = el("div", "detail-block");
        endpoints.append(el("div", "detail-title", "Endpoints"));
        endpoints.append(endpointLinks(device.id, device.endpoints, device.proxy_endpoints));

        const statusFields = el("div", "detail-block");
        statusFields.append(el("div", "detail-title", "Status Fields"));
        statusFields.append(keyValueRows(objectRows(device.status)));

        const lastResult = el("div", "detail-block");
        lastResult.append(el("div", "detail-title", "Last Result"));
        lastResult.append(keyValueRows([
          ["Command", device.last_command],
          ["Transcript", device.last_transcript],
          ["Display", device.last_display_text],
        ]));

        grid.append(identity, stateBlock, firmware, caps, endpoints, statusFields, lastResult);
        detail.append(grid);
        row.append(summary, detail);
        list.append(row);
      }
      root.append(list);
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

    function renderFirmwareCatalog(items) {
      const root = document.getElementById("firmwareCatalog");
      root.replaceChildren();
      if (!items || !items.length) {
        root.append(el("div", "empty", "No firmware catalog entries."));
        return;
      }
      for (const item of items) {
        const entry = el("div", "event");
        entry.append(el("div", "device-id", text(item.device_type)));
        entry.append(el("div", "meta", `latest ${text(item.latest_version)} | ${item.version_count || 0} version(s)`));
        if (item.latest && item.latest.url) {
          const link = el("a", "", text(item.latest.filename || "binary"));
          link.href = item.latest.url;
          link.target = "_blank";
          link.rel = "noreferrer";
          entry.append(link);
        }
        root.append(entry);
      }
    }

    function defaultActionTranscript(action) {
      if (action.name === "notify phone") return "run action notify phone message dashboard test";
      if (action.name === "send test email") return "run action send test email";
      if (action.name === "send email") return "run action send email to recipient@example.com subject test message dashboard test";
      return `run action ${action.name}`;
    }

    async function runAction(action, input, button, result) {
      const original = button.textContent;
      button.disabled = true;
      button.textContent = "Running";
      result.textContent = "";
      try {
        const payload = {
          transcript: input.value || defaultActionTranscript(action),
          device_id: "dashboard",
        };
        if (action.requires_confirmation) {
          if (!window.confirm(`Run action "${action.name}"?`)) {
            button.textContent = original;
            button.disabled = false;
            return;
          }
          payload.confirm = true;
        }
        const response = await fetch(`/actions/${encodeURIComponent(action.name)}/run`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload),
        });
        const body = await response.json();
        result.textContent = body.display_text || body.error || `HTTP ${response.status}`;
      } catch (error) {
        result.textContent = error.message;
      }
      button.textContent = original;
      button.disabled = false;
    }

    async function simulateTranscript() {
      const button = document.getElementById("simulateButton");
      const transcriptInput = document.getElementById("simulateTranscript");
      const deviceInput = document.getElementById("simulateDeviceId");
      const result = document.getElementById("simulateResult");
      const original = button.textContent;
      button.disabled = true;
      button.textContent = "Running";
      result.textContent = "";
      try {
        const response = await fetch("/commands/simulate", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            transcript: transcriptInput.value,
            device_id: deviceInput.value || "dashboard",
          }),
        });
        const body = await response.json();
        result.textContent = body.display_text || body.error || `HTTP ${response.status}`;
        await refresh();
      } catch (error) {
        result.textContent = error.message;
      }
      button.textContent = original;
      button.disabled = false;
    }

    function renderActions(actions) {
      const root = document.getElementById("actions");
      root.replaceChildren();
      if (!actions || !actions.length) {
        root.append(el("div", "empty", "No packaged actions configured."));
        return;
      }
      for (const action of actions) {
        const item = el("div", "event");
        item.append(el("div", "device-id", action.name));
        item.append(el("div", "meta", `${action.requires_confirmation ? "confirmation required" : "no confirmation"} | timeout ${action.timeout_seconds}s`));
        if (action.description) item.append(el("div", "", action.description));
        const form = el("div", "action-form");
        const input = document.createElement("input");
        input.type = "text";
        input.value = defaultActionTranscript(action);
        const row = el("div", "action-row");
        const button = el("button", "", "Run");
        button.type = "button";
        const result = el("span", "meta", "");
        button.addEventListener("click", () => runAction(action, input, button, result));
        row.append(button, result);
        form.append(input, row);
        item.append(form);
        root.append(item);
      }
    }

    function renderDiagnostics(items) {
      const root = document.getElementById("diagnostics");
      root.replaceChildren();
      for (const item of items || []) {
        const row = el("div", `diagnostic ${item.ok ? "ok" : ""}`);
        row.append(el("div", "device-id", item.name));
        row.append(el("div", "meta", item.detail));
        root.append(row);
      }
    }

    function renderTimerDeviceOptions(devices) {
      const select = document.getElementById("timerDevice");
      const current = select.value || "dashboard";
      select.replaceChildren(new Option("Dashboard", "dashboard"));
      for (const device of devices || []) {
        select.append(new Option(`${device.display_name || device.id} (${device.id})`, device.id));
      }
      select.value = Array.from(select.options).some((option) => option.value === current) ? current : "dashboard";
    }

    function todayText() {
      const now = new Date();
      const month = String(now.getMonth() + 1).padStart(2, "0");
      const day = String(now.getDate()).padStart(2, "0");
      return `${now.getFullYear()}-${month}-${day}`;
    }

    function dateKey(date) {
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      return `${date.getFullYear()}-${month}-${day}`;
    }

    function parseDateKey(value) {
      const match = String(value || "").match(/^(\\d{4})-(\\d{2})-(\\d{2})$/);
      if (!match) return null;
      return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    }

    function monthTitle(date) {
      return date.toLocaleDateString(undefined, {month: "long", year: "numeric"});
    }

    function openMissionFormModal() {
      document.getElementById("missionFormResult").textContent = "";
      if (!document.getElementById("missionDueDate").value) {
        document.getElementById("missionDueDate").value = latestMissionToday || todayText();
      }
      document.getElementById("missionModal").classList.add("open");
      document.getElementById("missionTitle").focus();
    }

    function closeMissionFormModal() {
      document.getElementById("missionModal").classList.remove("open");
      document.getElementById("missionFormResult").textContent = "";
    }

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
      try {
        const response = await fetch("/mission-board/tasks", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload),
        });
        const body = await response.json();
        if (!response.ok || !body.ok) throw new Error(body.error || `HTTP ${response.status}`);
        document.getElementById("missionTitle").value = "";
        document.getElementById("missionNotes").value = "";
        document.getElementById("missionDueDate").value = latestMissionToday || todayText();
        document.getElementById("missionType").value = "persistent";
        result.textContent = "Added";
        closeMissionFormModal();
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
        await refresh();
      } catch (error) {
        result.textContent = error.message;
      }
    }

    async function completeMissionTask(taskId, button) {
      const original = button.textContent;
      button.disabled = true;
      button.textContent = "Completing";
      try {
        const response = await fetch(`/mission-board/tasks/${encodeURIComponent(taskId)}/complete`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({completed_by: "home-dashboard"}),
        });
        const body = await response.json();
        if (!response.ok || !body.ok) throw new Error(body.error || `HTTP ${response.status}`);
        await refresh();
      } catch (error) {
        button.textContent = "Failed";
        setTimeout(() => { button.textContent = original; button.disabled = false; }, 1200);
        return;
      }
      button.textContent = original;
      button.disabled = false;
    }

    function missionTaskItem(task, today, compact = false) {
        const item = el("div", "event");
        item.append(el("div", "device-id", text(task.title)));
        item.append(el("div", "meta", task.task_type === "daily" ? `dated | ${text(task.due_date || today)}` : "persistent"));
        if (task.notes) item.append(el("div", "", text(task.notes)));
        if (!compact) {
          item.append(el("div", "meta", `${text(task.id)} | source ${text(task.source)} | created ${new Date((task.created_at || 0) * 1000).toLocaleString()}`));
        }
        const row = el("div", "action-row");
        const complete = el("button", "", "Complete");
        complete.type = "button";
        complete.addEventListener("click", () => completeMissionTask(task.id, complete));
        row.append(complete);
        item.append(row);
        return item;
    }

    function renderMissionCalendar(tasks, today) {
      const root = document.getElementById("missionCalendar");
      root.replaceChildren();
      document.getElementById("missionCalendarTitle").textContent = monthTitle(missionCalendarDate);
      const year = missionCalendarDate.getFullYear();
      const month = missionCalendarDate.getMonth();
      const first = new Date(year, month, 1);
      const start = new Date(first);
      start.setDate(first.getDate() - ((first.getDay() + 6) % 7));
      const byDate = new Map();
      for (const task of tasks) {
        if (task.task_type !== "daily") continue;
        const key = task.due_date || today;
        if (!byDate.has(key)) byDate.set(key, []);
        byDate.get(key).push(task);
      }
      for (let index = 0; index < 42; index += 1) {
        const date = new Date(start);
        date.setDate(start.getDate() + index);
        const key = dateKey(date);
        const day = el("div", `calendar-day${date.getMonth() === month ? "" : " outside"}${key === today ? " today" : ""}`);
        day.append(el("div", "calendar-date", String(date.getDate())));
        const dayTasks = byDate.get(key) || [];
        for (const task of dayTasks.slice(0, 3)) {
          const item = el("button", "calendar-task", task.title || "Untitled task");
          item.type = "button";
          item.title = task.notes ? `${task.title || "Untitled task"} - ${task.notes}` : (task.title || "Untitled task");
          item.addEventListener("click", () => setActiveTab("mission"));
          day.append(item);
        }
        if (dayTasks.length > 3) day.append(el("div", "calendar-more", `+${dayTasks.length - 3} more`));
        root.append(day);
      }
    }

    function renderMissionBoard(board) {
      const root = document.getElementById("missionTasks");
      const persistentRoot = document.getElementById("persistentMissionTasks");
      root.replaceChildren();
      persistentRoot.replaceChildren();
      const tasks = Array.isArray(board?.tasks) ? board.tasks : [];
      const today = board?.today || todayText();
      latestMissionToday = today;
      document.getElementById("missionCount").textContent = `${tasks.length} open`;
      document.getElementById("activeMissionCount").textContent = `${tasks.length} shown`;
      const todayDate = parseDateKey(today);
      if (todayDate && !missionCalendarDate) {
        missionCalendarDate = new Date(todayDate.getFullYear(), todayDate.getMonth(), 1);
      }
      renderMissionCalendar(tasks, today);

      const persistent = tasks.filter((task) => task.task_type !== "daily");
      document.getElementById("persistentMissionCount").textContent = `${persistent.length} open`;
      if (!persistent.length) {
        persistentRoot.append(el("div", "empty", "No persistent tasks."));
      } else {
        for (const task of persistent) persistentRoot.append(missionTaskItem(task, today, true));
      }

      if (!tasks.length) {
        root.append(el("div", "empty", "No open mission tasks."));
        return;
      }
      for (const task of tasks) root.append(missionTaskItem(task, today));
    }

    async function createTimer(event) {
      event.preventDefault();
      const result = document.getElementById("timerFormResult");
      const payload = {
        name: document.getElementById("timerName").value,
        duration_text: document.getElementById("timerDuration").value,
        mode: document.getElementById("timerMode").value,
        device_id: document.getElementById("timerDevice").value || "dashboard",
      };
      try {
        const response = await fetch("/timers", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload),
        });
        const body = await response.json();
        if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
        document.getElementById("timerDuration").value = "";
        result.textContent = "Started";
        await refresh();
      } catch (error) {
        result.textContent = error.message;
      }
    }

    async function cancelTimer(timerId, button) {
      const original = button.textContent;
      button.disabled = true;
      button.textContent = "Cancelling";
      try {
        const response = await fetch(`/timers/${encodeURIComponent(timerId)}`, {method: "DELETE"});
        const body = await response.json();
        if (!response.ok || !body.ok) throw new Error(body.error || `HTTP ${response.status}`);
        await refresh();
      } catch (error) {
        button.textContent = "Failed";
        setTimeout(() => { button.textContent = original; button.disabled = false; }, 1200);
        return;
      }
      button.textContent = original;
      button.disabled = false;
    }

    function renderTimers(timers, devices) {
      renderTimerDeviceOptions(devices);
      const root = document.getElementById("timers");
      root.replaceChildren();
      if (!timers || !timers.length) {
        root.append(el("div", "empty", "No active timers."));
        return;
      }
      for (const timer of timers) {
        const item = el("div", "event");
        item.append(el("div", "device-id", `${text(timer.display_name)} -> ${text(timer.mode)}`));
        item.append(el("div", "", `${text(timer.remaining_seconds)}s remaining of ${text(timer.duration_seconds)}s`));
        item.append(el("div", "meta", `${text(timer.device_name)} | ${text(timer.id)}`));
        item.append(el("div", "meta", `expires ${new Date(timer.expires_at * 1000).toLocaleTimeString()}`));
        const row = el("div", "action-row");
        const cancel = el("button", "danger", "Cancel");
        cancel.type = "button";
        cancel.addEventListener("click", () => cancelTimer(timer.id, cancel));
        row.append(cancel);
        item.append(row);
        root.append(item);
      }
    }

    function formatInterval(seconds) {
      seconds = Number(seconds || 0);
      if (seconds < 60) return `${seconds}s`;
      if (seconds % 3600 === 0) return `${seconds / 3600}h`;
      if (seconds % 60 === 0) return `${seconds / 60}m`;
      return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
    }

    function formatBytes(bytes) {
      if (!Number.isFinite(bytes)) return "-";
      const units = ["B", "KB", "MB", "GB", "TB"];
      let value = Number(bytes);
      let unit = 0;
      while (value >= 1024 && unit < units.length - 1) {
        value /= 1024;
        unit += 1;
      }
      return `${value >= 10 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
    }

    function formatPercent(value) {
      return Number.isFinite(value) ? `${Number(value).toFixed(1)}%` : "-";
    }

    function metricCard(label, value, percent, detail = "") {
      const card = el("div", "metric-card");
      const head = el("div", "metric-head");
      head.append(el("span", "", label));
      head.append(el("span", "", formatPercent(percent)));
      card.append(head);
      card.append(el("div", "metric-value", value));
      const meter = el("div", "meter");
      const fill = el("div", "meter-fill");
      fill.style.width = `${Math.max(0, Math.min(100, Number(percent) || 0))}%`;
      meter.append(fill);
      card.append(meter);
      if (detail) card.append(el("div", "meta", detail));
      return card;
    }

    function renderServerDetails(server) {
      const system = server?.system || {};
      const metricsRoot = document.getElementById("serverMetrics");
      const detailsRoot = document.getElementById("serverDetails");
      const gpuRoot = document.getElementById("serverGpu");
      const gpuPanel = document.getElementById("serverGpuPanel");
      metricsRoot.replaceChildren();
      detailsRoot.replaceChildren();
      gpuRoot.replaceChildren();

      const cpu = system.cpu || {};
      const memory = system.memory || {};
      metricsRoot.append(metricCard(
        "CPU",
        cpu.usage_percent === null || cpu.usage_percent === undefined ? "Sampling" : formatPercent(cpu.usage_percent),
        cpu.usage_percent,
        cpu.load_average ? `load ${cpu.load_average["1m"]} / ${cpu.load_average["5m"]} / ${cpu.load_average["15m"]} | ${text(cpu.core_count)} core(s)` : `${text(cpu.core_count)} core(s)`
      ));
      metricsRoot.append(metricCard(
        "RAM",
        `${formatBytes(memory.used_bytes)} / ${formatBytes(memory.total_bytes)}`,
        memory.used_percent,
        `${formatBytes(memory.available_bytes)} available`
      ));
      if (Number(memory.swap_total_bytes || 0) > 0) {
        metricsRoot.append(metricCard(
          "Swap",
          `${formatBytes(memory.swap_used_bytes)} / ${formatBytes(memory.swap_total_bytes)}`,
          memory.swap_used_percent
        ));
      }
      for (const disk of system.storage || []) {
        metricsRoot.append(metricCard(
          disk.label || disk.path || "Storage",
          disk.error ? "Unavailable" : `${formatBytes(disk.used_bytes)} / ${formatBytes(disk.total_bytes)}`,
          disk.used_percent,
          disk.error || `${formatBytes(disk.free_bytes)} free | ${text(disk.path)}`
        ));
      }

      const details = el("div", "event");
      details.append(keyValueRows([
        ["Hostname", system.hostname],
        ["Platform", system.platform],
        ["Python", system.python],
        ["Server", `${server.host}:${server.port}`],
        ["Started", server.started_at ? new Date(server.started_at * 1000).toLocaleString() : ""],
        ["Uptime", uptime(server.uptime_seconds)],
        ["Collected", system.collected_at ? new Date(system.collected_at * 1000).toLocaleTimeString() : ""],
      ]));
      detailsRoot.append(details);

      const gpus = system.gpus || [];
      if (!gpus.length) {
        if (gpuPanel) gpuPanel.style.display = "none";
        return;
      }
      if (gpuPanel) gpuPanel.style.display = "";
      for (const gpu of gpus) {
        const item = el("div", "event");
        item.append(el("div", "device-id", text(gpu.name || gpu.id)));
        item.append(el("div", "meta", `${text(gpu.provider)} | usage ${formatPercent(gpu.usage_percent)}`));
        if (gpu.memory_total_bytes) {
          item.append(el("div", "meta", `memory ${formatBytes(gpu.memory_used_bytes)} / ${formatBytes(gpu.memory_total_bytes)} (${formatPercent(gpu.memory_used_percent)})`));
        }
        if (gpu.temperature_c !== null && gpu.temperature_c !== undefined) {
          item.append(el("div", "meta", `temperature ${gpu.temperature_c} C`));
        }
        gpuRoot.append(item);
      }
    }

    async function createUptimeMonitor(event) {
      event.preventDefault();
      const result = document.getElementById("uptimeFormResult");
      const payload = {
        id: document.getElementById("uptimeId").value,
        name: document.getElementById("uptimeName").value,
        target: document.getElementById("uptimeTarget").value,
        interval_seconds: Number(document.getElementById("uptimeInterval").value || 600),
        enabled: true,
      };
      try {
        const response = await fetch("/uptime", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload),
        });
        const body = await response.json();
        if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
        resetUptimeForm();
        result.textContent = payload.id ? "Saved" : "Added";
        closeUptimeFormModal();
        await refresh();
      } catch (error) {
        result.textContent = error.message;
      }
    }

    function openUptimeFormModal() {
      document.getElementById("uptimeModal").classList.add("open");
      document.getElementById("uptimeName").focus();
    }

    function closeUptimeFormModal() {
      document.getElementById("uptimeModal").classList.remove("open");
      document.getElementById("uptimeFormResult").textContent = "";
    }

    function resetUptimeForm() {
      document.getElementById("uptimeId").value = "";
      document.getElementById("uptimeName").value = "";
      document.getElementById("uptimeTarget").value = "";
      document.getElementById("uptimeInterval").value = "600";
      document.getElementById("uptimeSubmitButton").textContent = "Register";
      document.getElementById("uptimeCancelEditButton").style.display = "none";
    }

    function editUptimeMonitor(monitor) {
      document.getElementById("uptimeId").value = monitor.id || "";
      document.getElementById("uptimeName").value = monitor.name || "";
      document.getElementById("uptimeTarget").value = monitor.target || "";
      document.getElementById("uptimeInterval").value = monitor.interval_seconds || 600;
      document.getElementById("uptimeSubmitButton").textContent = "Save";
      document.getElementById("uptimeCancelEditButton").style.display = "";
      document.getElementById("uptimeFormResult").textContent = "";
      setActiveTab("uptime");
      openUptimeFormModal();
      document.getElementById("uptimeInterval").focus();
    }

    async function checkUptimeMonitor(monitorId, button) {
      const original = button.textContent;
      button.disabled = true;
      button.textContent = "Checking";
      try {
        const response = await fetch(`/uptime/${encodeURIComponent(monitorId)}/check`, {method: "POST"});
        const body = await response.json();
        if (!response.ok || !body.ok) throw new Error(body.error || `HTTP ${response.status}`);
        await refresh();
      } catch (_error) {
        button.textContent = "Failed";
        setTimeout(() => { button.textContent = original; button.disabled = false; }, 1200);
        return;
      }
      button.textContent = original;
      button.disabled = false;
    }

    async function deleteUptimeMonitor(monitorId) {
      if (!window.confirm(`Remove uptime monitor ${monitorId}?`)) return;
      await fetch(`/uptime/${encodeURIComponent(monitorId)}`, {method: "DELETE"});
      await refresh();
    }

    function renderUptimeMonitors(monitors) {
      const root = document.getElementById("uptimeMonitors");
      root.replaceChildren();
      if (!monitors || !monitors.length) {
        root.append(el("div", "empty", "No uptime monitors registered."));
        return;
      }
      const sorted = [...monitors].sort((a, b) => {
        if (a.online !== b.online) return a.online ? 1 : -1;
        return String(a.name || a.target).localeCompare(String(b.name || b.target));
      });
      for (const monitor of sorted) {
        const item = el("div", "event");
        const title = el("div", "device-id", text(monitor.name || monitor.target));
        const status = el("span", `status ${monitor.online ? "online" : ""}`);
        status.append(el("span", "dot"));
        status.append(el("span", "", monitor.online ? "Up" : "Down"));
        item.append(title);
        item.append(status);
        item.append(el("div", "meta", `${text(monitor.target)} | every ${formatInterval(monitor.interval_seconds)}`));
        item.append(el("div", "meta", `last: ${monitor.last_checked_at ? new Date(monitor.last_checked_at * 1000).toLocaleString() : "not checked"} | next: ${monitor.next_check_at ? new Date(monitor.next_check_at * 1000).toLocaleTimeString() : "-"}`));
        item.append(el("div", "", `${text(monitor.detail)}${monitor.latency_ms !== null && monitor.latency_ms !== undefined ? ` | ${monitor.latency_ms} ms` : ""}`));
        const row = el("div", "action-row");
        const check = el("button", "", "Check Now");
        check.type = "button";
        check.addEventListener("click", () => checkUptimeMonitor(monitor.id, check));
        const edit = el("button", "", "Edit");
        edit.type = "button";
        edit.addEventListener("click", () => editUptimeMonitor(monitor));
        const remove = el("button", "danger", "Remove");
        remove.type = "button";
        remove.addEventListener("click", () => deleteUptimeMonitor(monitor.id));
        row.append(check, edit, remove);
        item.append(row);
        root.append(item);
      }
    }

    async function saveRule(form, result) {
      const data = new FormData(form);
      const steps = String(data.get("steps") || "")
        .split("\\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
          if (line.toLowerCase().startsWith("action:")) {
            const value = line.slice(7).trim();
            return {action_type: "action", action_name: value, action_transcript: `run action ${value}`};
          }
          if (line.toLowerCase().startsWith("transcript:")) {
            return {action_type: "transcript", transcript: line.slice(11).trim()};
          }
          return {action_type: "transcript", transcript: line};
        });
      const payload = {
        id: data.get("id"),
        enabled: data.get("enabled") === "on",
        name: data.get("name"),
        event_type: data.get("event_type"),
        device_id: data.get("device_id"),
        button: data.get("button"),
        capability: data.get("capability"),
        command: data.get("command"),
        steps,
      };
      try {
        const response = await fetch("/rules", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload),
        });
        const body = await response.json();
        if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
        result.textContent = "Saved";
        await refresh();
      } catch (error) {
        result.textContent = error.message;
      }
    }

    async function toggleRule(rule) {
      await fetch("/rules", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({...rule, enabled: !rule.enabled}),
      });
      await refresh();
    }

    async function testRule(ruleId) {
      await fetch(`/rules/${encodeURIComponent(ruleId)}/test`, {method: "POST"});
      await refresh();
    }

    async function deleteRule(ruleId) {
      if (!window.confirm(`Remove rule ${ruleId}?`)) return;
      await fetch(`/rules/${encodeURIComponent(ruleId)}`, {method: "DELETE"});
      await refresh();
    }

    function stepsText(rule) {
      const steps = rule.steps && rule.steps.length ? rule.steps : [{action_type: rule.action_type, transcript: rule.transcript, action_name: rule.action_name}];
      return steps.map((step) => {
        if (step.action_type === "action") return `action: ${step.action_name || ""}`;
        return `transcript: ${step.transcript || ""}`;
      }).join("\\n");
    }

    function populateRuleForm(form, rule, devices) {
      form.querySelector('[name="id"]').value = rule.id || "";
      form.querySelector('[name="name"]').value = rule.name || "";
      form.querySelector('[name="event_type"]').value = rule.event_type || "button";
      form.querySelector('[name="button"]').value = rule.button || "";
      form.querySelector('[name="capability"]').value = rule.capability || "";
      form.querySelector('[name="command"]').value = rule.command || "";
      form.querySelector('[name="enabled"]').checked = rule.enabled !== false;
      form.querySelector('[name="steps"]').value = stepsText(rule);
      const deviceSelect = form.querySelector('select[name="device_id"]');
      deviceSelect.replaceChildren(new Option("Any device", ""));
      for (const device of devices || []) {
        deviceSelect.append(new Option(`${device.display_name || device.id} (${device.id})`, device.id));
      }
      deviceSelect.value = rule.device_id || "";
    }

    function ruleForm(rule, devices, title) {
      const formItem = el("div", "event");
      formItem.append(el("div", "device-id", title));
      const form = el("form", "action-form");
      form.innerHTML = `
        <input name="id" type="hidden">
        <input name="name" type="text" placeholder="Rule name">
        <select name="event_type">
          <option value="button">Button event</option>
          <option value="device_online">Device online</option>
          <option value="device_offline">Device offline</option>
          <option value="low_battery">Low battery</option>
          <option value="timer_complete">Timer complete</option>
          <option value="command">Voice/simulated command</option>
          <option value="camera_event">Camera event</option>
        </select>
        <select name="device_id"></select>
        <input name="button" type="text" placeholder="Button filter, optional">
        <input name="capability" type="text" placeholder="Capability filter, optional">
        <input name="command" type="text" placeholder="Command filter, optional">
        <textarea name="steps" placeholder="One action per line. Use: transcript: set timer for 5 minutes notify phone OR action: notify phone"></textarea>
        <label class="meta"><input name="enabled" type="checkbox" checked> Enabled</label>
        <div class="action-row"><button type="submit">Save</button><span class="meta"></span></div>
      `;
      populateRuleForm(form, rule, devices);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        saveRule(form, form.querySelector(".action-row .meta"));
      });
      formItem.append(form);
      return formItem;
    }

    function renderRules(rules, devices, actions, runs) {
      const root = document.getElementById("rules");
      root.replaceChildren();

      root.append(ruleForm({
        id: "",
        name: "Button rule",
        event_type: "button",
        enabled: true,
        steps: [{action_type: "transcript", transcript: "set timer for 5 minutes notify phone"}],
      }, devices, "New event rule"));

      if (!rules || !rules.length) {
        root.append(el("div", "empty", "No event rules configured."));
      } else {
        for (const rule of rules) {
          const item = el("div", "event");
          item.append(el("div", "device-id", rule.name || rule.id));
          item.append(el("div", "meta", `${rule.enabled ? "enabled" : "disabled"} | ${rule.event_type} | ${rule.device_id || "any device"} | ${rule.button || "any button"}`));
          item.append(el("div", "", `Steps: ${stepsText(rule).replaceAll("\\n", " | ")}`));
          if (rule.last_result) item.append(el("div", "meta", `last: ${rule.last_result}`));
          const row = el("div", "action-row");
          const edit = el("button", "", "Edit");
          edit.type = "button";
          edit.addEventListener("click", () => item.replaceWith(ruleForm(rule, devices, `Edit: ${rule.name || rule.id}`)));
          const toggle = el("button", "", rule.enabled ? "Disable" : "Enable");
          toggle.type = "button";
          toggle.addEventListener("click", () => toggleRule(rule));
          const test = el("button", "", "Test");
          test.type = "button";
          test.addEventListener("click", () => testRule(rule.id));
          const remove = el("button", "danger", "Remove");
          remove.type = "button";
          remove.addEventListener("click", () => deleteRule(rule.id));
          row.append(edit, toggle, test, remove);
          item.append(row);
          root.append(item);
        }
      }

      const recent = (runs || []).slice(-3).reverse();
      for (const run of recent) {
        const item = el("div", "event");
        item.append(el("div", "device-id", `Rule run: ${run.rule_id}`));
        item.append(el("div", "meta", `${run.ok ? "ok" : "failed"} | ${text(run.device_id)} | ${new Date(run.received_at * 1000).toLocaleTimeString()}`));
        item.append(el("div", "", text(run.result)));
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

    function configureRestartButton(server) {
      const button = document.getElementById("restartButton");
      const enabled = Boolean(server && server.restart_enabled);
      button.disabled = !enabled;
      button.title = enabled ? "Restart spoken-command-server" : "Restart is disabled in server configuration";
    }

    async function restartServer() {
      const button = document.getElementById("restartButton");
      if (button.disabled) return;
      if (!confirm("Restart spoken-command-server now?")) return;
      const original = button.textContent;
      button.disabled = true;
      button.textContent = "Restarting...";
      try {
        const response = await fetch("/server/restart", {method: "POST"});
        const body = await response.json();
        if (!response.ok || !body.ok) throw new Error(body.error || `HTTP ${response.status}`);
        document.getElementById("refreshState").textContent = "Restart requested";
      } catch (error) {
        document.getElementById("refreshState").textContent = error.message;
        button.disabled = false;
        button.textContent = original;
      }
    }

    function renderR1Note(note) {
      const input = document.getElementById("r1NoteText");
      const meta = document.getElementById("r1NoteMeta");
      const value = note?.text || "";
      if (document.activeElement !== input && input.value !== value) {
        input.value = value;
      }
      meta.textContent = note?.updated_at ? `Updated ${new Date(note.updated_at * 1000).toLocaleString()}` : "Not set";
    }

    async function saveR1Note(event) {
      event.preventDefault();
      const result = document.getElementById("r1NoteMeta");
      result.textContent = "Saving...";
      try {
        const response = await fetch("/r1-note", {
          method: "PUT",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({text: document.getElementById("r1NoteText").value}),
        });
        const body = await response.json();
        if (!response.ok || body.ok === false) throw new Error(body.error || `HTTP ${response.status}`);
        renderR1Note(body.r1_note);
      } catch (error) {
        result.textContent = error.message;
      }
    }

    function render(data) {
      document.getElementById("serverMeta").textContent =
        `Listening on ${data.server.host}:${data.server.port} | uptime ${uptime(data.server.uptime_seconds)} | stale after ${data.server.device_stale_seconds}s`;
      configureRestartButton(data.server);
      renderR1Note(data.r1_note);
      renderStats(data);
      renderServerDetails(data.server);
      renderAttention(data.devices);
      renderActivity(data);
      renderDiagnostics(data.server.diagnostics);
      renderActions(data.actions);
      renderRules(data.rules, data.devices, data.actions, data.recent_rule_runs);
      renderMissionBoard(data.mission_board);
      renderTimers(data.active_timers, data.devices);
      renderUptimeMonitors(data.uptime_monitors);
      state.devices = data.devices || [];
      populateDeviceFilterOptions(state.devices);
      renderFilteredDevices();
      renderFirmwareCatalog(data.firmware_catalog);
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
      if (document.activeElement && document.activeElement.closest(".name-form, .action-form")) {
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
    document.getElementById("simulateButton").addEventListener("click", simulateTranscript);
    document.getElementById("restartButton").addEventListener("click", restartServer);
    document.getElementById("r1NoteForm").addEventListener("submit", saveR1Note);
    document.getElementById("missionForm").addEventListener("submit", createMissionTask);
    document.getElementById("openMissionForm").addEventListener("click", openMissionFormModal);
    document.getElementById("closeMissionForm").addEventListener("click", closeMissionFormModal);
    document.getElementById("cancelMissionForm").addEventListener("click", closeMissionFormModal);
    document.getElementById("missionModal").addEventListener("click", (event) => {
      if (event.target.id === "missionModal") closeMissionFormModal();
    });
    document.getElementById("missionCalendarPrev").addEventListener("click", () => {
      missionCalendarDate = new Date(missionCalendarDate.getFullYear(), missionCalendarDate.getMonth() - 1, 1);
      refresh();
    });
    document.getElementById("missionCalendarToday").addEventListener("click", () => {
      const today = parseDateKey(todayText()) || new Date();
      missionCalendarDate = new Date(today.getFullYear(), today.getMonth(), 1);
      refresh();
    });
    document.getElementById("missionCalendarNext").addEventListener("click", () => {
      missionCalendarDate = new Date(missionCalendarDate.getFullYear(), missionCalendarDate.getMonth() + 1, 1);
      refresh();
    });
    document.getElementById("missionDueDate").value = todayText();
    document.getElementById("timerForm").addEventListener("submit", createTimer);
    document.getElementById("uptimeForm").addEventListener("submit", createUptimeMonitor);
    document.getElementById("openUptimeForm").addEventListener("click", () => {
      resetUptimeForm();
      openUptimeFormModal();
    });
    document.getElementById("closeUptimeForm").addEventListener("click", closeUptimeFormModal);
    document.getElementById("cancelUptimeForm").addEventListener("click", closeUptimeFormModal);
    document.getElementById("uptimeCancelEditButton").addEventListener("click", () => {
      resetUptimeForm();
      closeUptimeFormModal();
    });
    document.getElementById("uptimeModal").addEventListener("click", (event) => {
      if (event.target.id === "uptimeModal") closeUptimeFormModal();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (document.getElementById("missionModal").classList.contains("open")) closeMissionFormModal();
      if (document.getElementById("uptimeModal").classList.contains("open")) closeUptimeFormModal();
    });
    document.getElementById("simulateTranscript").addEventListener("keydown", (event) => {
      if (event.key === "Enter") simulateTranscript();
    });
    document.getElementById("cancelRemoveButton").addEventListener("click", closeRemoveModal);
    document.getElementById("confirmRemoveButton").addEventListener("click", removePendingDevice);
    document.getElementById("removeModal").addEventListener("click", (event) => {
      if (event.target.id === "removeModal") closeRemoveModal();
    });
    initCollapsiblePanels();
    initTabs();
    initExpandedDevices();
    initDeviceFilters();
    resetUptimeForm();
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
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      color-scheme: dark;
      --bg: #0a0e15;
      --panel: #0f1620;
      --raised: #16202e;
      --text: #d7e1ee;
      --bright: #f2f7fc;
      --muted: #76879c;
      --line: #1d2939;
      --line-bright: #2c3d55;
      --accent: #41d6c5;
      --accent-soft: rgba(65, 214, 197, 0.12);
      --accent-glow: rgba(65, 214, 197, 0.35);
      --good: #4ade83;
      --bad: #ff6b5e;
      --display: "Chakra Petch", "Segoe UI", sans-serif;
      --mono: "IBM Plex Mono", ui-monospace, Consolas, monospace;
      --body: "IBM Plex Sans", system-ui, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--text);
      font: 14px/1.45 var(--body);
      background:
        radial-gradient(1100px 520px at 85% -10%, rgba(65, 214, 197, 0.07), transparent 60%),
        repeating-linear-gradient(0deg, rgba(65, 214, 197, 0.022) 0 1px, transparent 1px 28px),
        repeating-linear-gradient(90deg, rgba(65, 214, 197, 0.022) 0 1px, transparent 1px 28px),
        var(--bg);
      background-attachment: fixed;
    }
    ::selection { background: var(--accent); color: #06251f; }
    @keyframes deck-rise {
      from { opacity: 0; transform: translateY(10px); }
      to { opacity: 1; transform: translateY(0); }
    }
    @keyframes deck-pulse {
      0%, 100% { box-shadow: 0 0 0 0 var(--accent-glow); }
      50% { box-shadow: 0 0 8px 2px var(--accent-glow); }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { animation: none !important; transition: none !important; }
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 16px 22px;
      background: linear-gradient(180deg, rgba(15, 22, 32, 0.96), rgba(15, 22, 32, 0.88));
      backdrop-filter: blur(6px);
      border-bottom: 1px solid var(--line);
      box-shadow: 0 1px 0 rgba(65, 214, 197, 0.14), 0 12px 30px rgba(2, 6, 12, 0.45);
      position: sticky;
      top: 0;
      z-index: 1;
      animation: deck-rise 0.4s ease-out backwards;
    }
    h1 {
      margin: 0;
      font-family: var(--display);
      font-size: 19px;
      font-weight: 700;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--bright);
    }
    h1::before {
      content: "";
      display: inline-block;
      width: 9px;
      height: 16px;
      margin-right: 12px;
      background: var(--accent);
      clip-path: polygon(0 0, 100% 0, 100% 70%, 0 100%);
      box-shadow: 0 0 12px var(--accent-glow);
      vertical-align: -2px;
    }
    main {
      max-width: 1440px;
      margin: 0 auto;
      padding: 20px;
      animation: deck-rise 0.5s ease-out 0.1s backwards;
    }
    button, select {
      border: 1px solid var(--line-bright);
      background: var(--raised);
      color: var(--text);
      height: 34px;
      padding: 0 12px;
      border-radius: 3px;
      font-family: var(--body);
      font-size: 13.5px;
    }
    button {
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
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; text-shadow: 0 0 12px var(--accent-glow); }
    .toolbar {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      color: var(--muted);
      flex-wrap: wrap;
      font-family: var(--mono);
      font-size: 12.5px;
    }
    .meta {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11.5px;
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
      border-radius: 4px;
      overflow: hidden;
      animation: deck-rise 0.45s ease-out backwards;
      transition: border-color 0.15s, box-shadow 0.15s;
    }
    .camera:nth-child(2) { animation-delay: 0.06s; }
    .camera:nth-child(3) { animation-delay: 0.12s; }
    .camera:nth-child(4) { animation-delay: 0.18s; }
    .camera:nth-child(5) { animation-delay: 0.24s; }
    .camera:nth-child(6) { animation-delay: 0.3s; }
    .camera:hover { border-color: var(--accent); box-shadow: 0 0 16px var(--accent-soft); }
    .frame {
      position: relative;
      background:
        repeating-linear-gradient(0deg, rgba(65, 214, 197, 0.03) 0 1px, transparent 1px 3px),
        #04060a;
      aspect-ratio: 4 / 3;
      display: grid;
      place-items: center;
      border-bottom: 1px solid var(--line);
    }
    .frame img {
      width: 100%;
      height: 100%;
      object-fit: contain;
      display: block;
    }
    .frame .empty {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12.5px;
      padding: 18px;
      text-align: center;
    }
    .info { display: grid; gap: 4px; padding: 12px; }
    .title-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .device-id {
      font-weight: 600;
      word-break: break-word;
      color: var(--bright);
    }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      color: var(--muted);
      white-space: nowrap;
      font-family: var(--mono);
      font-size: 12px;
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--bad);
      box-shadow: 0 0 7px rgba(255, 107, 94, 0.6);
    }
    .online .dot {
      background: var(--good);
      box-shadow: 0 0 7px rgba(74, 222, 131, 0.7);
      animation: deck-pulse 2.4s ease-in-out infinite;
    }
    .links { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 4px; }
    .empty-page {
      border: 1px solid var(--line);
      border-radius: 4px;
      background: var(--panel);
      color: var(--muted);
      font-family: var(--mono);
      padding: 22px;
    }
    ::-webkit-scrollbar { width: 10px; height: 10px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--line-bright); border-radius: 5px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--accent); }
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

        immediate_commands = {"mute", "mute all", "unmute", "unmute all", "status", "server status", "list devices", "devices", "device list", "show devices", "list timers", "show timers", "timers", "active timers", "ping", "ping devices", "check devices", "check all devices", "help", "commands", "what can you do", "cancel", "stop", "cancel timer", "stop timer", "nevermind", "never mind"}
        named_cancel = re.match(r"^(?:cancel|stop)\s+(.+?)\s+timer$", normalized)
        if named_cancel:
            return handle_cancel_timer(text, device_id, named_cancel.group(1))

        if normalized in immediate_commands or normalized.startswith("broadcast "):
            command = dispatch_command(text, device_id)
            if command is not None:
                return command

        pending_response = handle_pending_action(device_id, text, normalized)
        if pending_response is not None:
            return pending_response

        if re.match(r"^(?:set|start)\s+(?:a\s+)?\S.+\s+timer\b", normalized):
            return handle_timer(text, device_id, text)

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
        if parsed.path == "/actions":
            with STATE_LOCK:
                payload = {"actions": list_script_actions()}
            json_response(self, 200, payload)
            return
        if parsed.path == "/rules":
            with STATE_LOCK:
                payload = {
                    "rules": [public_rule(rule_id, EVENT_RULES[rule_id]) for rule_id in sorted(EVENT_RULES)],
                    "recent_rule_runs": RECENT_RULE_RUNS[-20:],
                }
            json_response(self, 200, payload)
            return
        if parsed.path == "/timers":
            with STATE_LOCK:
                payload = {"timers": active_timer_summary()}
            json_response(self, 200, payload)
            return
        if parsed.path == "/uptime":
            with STATE_LOCK:
                payload = {"monitors": [public_uptime_monitor(UPTIME_MONITORS[monitor_id]) for monitor_id in sorted(UPTIME_MONITORS)]}
            json_response(self, 200, payload)
            return
        if parsed.path == "/mission-board":
            with STATE_LOCK:
                payload = mission_board_summary()
            json_response(self, 200, payload)
            return
        if parsed.path == "/r1-note":
            with STATE_LOCK:
                payload = public_r1_note()
            json_response(self, 200, payload)
            return
        if parsed.path == "/r1-update":
            json_response(self, 200, r1_update_manifest())
            return
        if parsed.path.startswith("/r1-apk/"):
            filename = parsed.path.removeprefix("/r1-apk/")
            try:
                cleaned_filename, path = r1_apk_path(filename)
            except ValueError as exc:
                json_response(self, 404, {"ok": False, "error": str(exc)})
                return
            if not os.path.isfile(path):
                json_response(self, 404, {"ok": False, "error": "APK not found"})
                return
            with open(path, "rb") as handle:
                body = handle.read()
            binary_response(self, 200, "application/vnd.android.package-archive", body, {
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{cleaned_filename}"',
            })
            return
        if parsed.path == "/events/recent":
            with STATE_LOCK:
                payload = {"recent_rule_runs": RECENT_RULE_RUNS[-20:]}
            json_response(self, 200, payload)
            return
        if parsed.path == "/firmware/catalog":
            with STATE_LOCK:
                payload = {"firmware": firmware_catalog_summary()}
            json_response(self, 200, payload)
            return
        if parsed.path.startswith("/firmware/catalog/"):
            device_type = clean_device_type(parsed.path.removeprefix("/firmware/catalog/"))
            with STATE_LOCK:
                entry = FIRMWARE_CATALOG.get(device_type)
            if not entry:
                json_response(self, 404, {"error": "firmware catalog entry not found", "device_type": device_type})
                return
            json_response(self, 200, {"device_type": device_type, "firmware": entry})
            return
        if parsed.path.startswith("/firmware/bin/"):
            parts = parsed.path.strip("/").split("/", 4)
            if len(parts) != 5:
                json_response(self, 404, {"error": "expected /firmware/bin/{device_type}/{version}/{filename}"})
                return
            _firmware, _bin, device_type, version, filename = parts
            path = firmware_binary_path(device_type, version, filename)
            if not os.path.isfile(path):
                json_response(self, 404, {"error": "firmware binary not found"})
                return
            with open(path, "rb") as handle:
                body = handle.read()
            binary_response(self, 200, "application/octet-stream", body, {
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{clean_filename(filename)}"',
            })
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

        if parsed.path == "/server/restart":
            try:
                schedule_server_restart()
                json_response(self, 202, {"ok": True, "message": "restart scheduled"})
            except Exception as exc:
                json_response(self, 403, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/commands/simulate":
            try:
                payload = read_optional_json_body(self)
                transcript_text = str(payload.get("transcript", "")).strip()[:500]
                device_id = clean_device_id(str(payload.get("device_id", "dashboard")))
                if not transcript_text:
                    raise ValueError("transcript is required")
                with STATE_LOCK:
                    touch_device(device_id, self)
                device_response = command_response(transcript_text, device_id)
                record = {
                    "device_id": device_id,
                    "received_at": int(time.time()),
                    "duration_ms": 0,
                    "ok": bool(device_response.get("ok")),
                    "text": transcript_text,
                    "display_text": device_response["display_text"],
                    "tone": device_response["tone"],
                    "command": device_response.get("command"),
                    "state": device_response.get("state", {}),
                    "muted": MUTED_DEVICES.get(device_id, False),
                    "transcript": {"text": transcript_text, "source": "simulated"},
                }
                with STATE_LOCK:
                    record_command_result(record)
                payload = dict(device_response)
                payload["simulated"] = True
                json_response(self, 200 if payload.get("ok") else 400, payload)
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/mission-board/tasks":
            try:
                payload = read_optional_json_body(self)
                with STATE_LOCK:
                    task = create_mission_task(payload, source="local")
                    board = mission_board_summary()
                json_response(self, 200, {"ok": True, "task": task, "mission_board": board})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path.startswith("/mission-board/tasks/") and parsed.path.endswith("/complete"):
            task_id = clean_rule_id(parsed.path.removeprefix("/mission-board/tasks/").removesuffix("/complete"))
            try:
                payload = read_optional_json_body(self)
                completed_by = str(payload.get("completed_by", "local"))
                with STATE_LOCK:
                    task = complete_mission_task(task_id, completed_by=completed_by)
                    board = mission_board_summary()
                json_response(self, 200, {"ok": True, "task": task, "mission_board": board})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/r1-note":
            try:
                payload = read_optional_json_body(self)
                with STATE_LOCK:
                    note = set_r1_note(payload, updated_by="api")
                json_response(self, 200, {"ok": True, "r1_note": note})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path.startswith("/actions/") and parsed.path.endswith("/run"):
            raw_name = unquote(parsed.path.removeprefix("/actions/").removesuffix("/run")).strip("/")
            try:
                payload = read_optional_json_body(self)
                device_id = clean_device_id(str(payload.get("device_id", "api")))
                transcript = str(payload.get("transcript", f"run action {raw_name}"))[:500]
                action_name = resolve_script_action(raw_name)
                if not action_name:
                    json_response(self, 404, {"ok": False, "error": "action not found", "actions": list_script_actions()})
                    return
                action = SCRIPT_ACTIONS[action_name]
                if action.get("requires_confirmation") and payload.get("confirm") is not True:
                    json_response(self, 409, {
                        "ok": False,
                        "error": "action requires confirmation",
                        "action": action_public_metadata(action_name, action),
                    })
                    return
                response = script_action_response(transcript, device_id, action_name)
                json_response(self, 200 if response.get("ok") else 500, response)
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/firmware/catalog":
            try:
                body = read_request_body(self)
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("firmware catalog body must be a JSON object")
                device_type = clean_device_type(str(payload.get("device_type", "")))
                version = clean_version(str(payload.get("version", "")))
                metadata = {
                    "filename": clean_filename(str(payload.get("filename", "firmware.bin"))),
                    "size": payload.get("size"),
                    "sha256": str(payload.get("sha256", ""))[:128],
                    "url": str(payload.get("url", ""))[:240],
                    "notes": str(payload.get("notes", ""))[:500],
                }
                with STATE_LOCK:
                    record = add_firmware_catalog_entry(device_type, version, metadata)
                json_response(self, 200, {"ok": True, "firmware": record})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

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

        if parsed.path == "/events":
            try:
                payload = read_optional_json_body(self)
                event_type = clean_rule_type(str(payload.get("event_type", payload.get("type", "custom"))))
                device_id = clean_device_id(str(payload.get("device_id", ""))) if payload.get("device_id") else ""
                extra = {
                    str(key)[:40]: value
                    for key, value in payload.items()
                    if key not in {"event_type", "type", "device_id"} and isinstance(value, (str, int, float, bool))
                }
                with STATE_LOCK:
                    dispatch_server_event(event_type, device_id, **extra)
                json_response(self, 200, {"ok": True, "event_type": event_type, "device_id": device_id})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/rules":
            try:
                payload = read_optional_json_body(self)
                with STATE_LOCK:
                    rule = upsert_event_rule(payload)
                json_response(self, 200, {"ok": True, "rule": rule})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/timers":
            try:
                payload = read_optional_json_body(self)
                with STATE_LOCK:
                    timer = create_timer_from_payload(payload)
                    summary = active_timer_summary()
                json_response(self, 200, {"ok": True, "timer": timer, "timers": summary})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path == "/uptime":
            try:
                payload = read_optional_json_body(self)
                with STATE_LOCK:
                    monitor = upsert_uptime_monitor(payload)
                json_response(self, 200, {"ok": True, "monitor": monitor})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path.startswith("/uptime/") and parsed.path.endswith("/check"):
            monitor_id = clean_rule_id(parsed.path.removeprefix("/uptime/").removesuffix("/check"))
            try:
                with STATE_LOCK:
                    monitor = dict(UPTIME_MONITORS.get(monitor_id, {}))
                if not monitor:
                    json_response(self, 404, {"ok": False, "error": "monitor not found"})
                    return
                result = check_uptime_monitor(monitor)
                with STATE_LOCK:
                    current = UPTIME_MONITORS[monitor_id]
                    now = int(time.time())
                    current.update(result)
                    current["last_checked_at"] = now
                    current["next_check_at"] = now + max(30, int(current.get("interval_seconds", 600) or 600))
                    save_uptime_monitors()
                    public = public_uptime_monitor(current)
                json_response(self, 200, {"ok": True, "monitor": public})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return

        if parsed.path.startswith("/rules/") and parsed.path.endswith("/test"):
            rule_id = clean_rule_id(parsed.path.removeprefix("/rules/").removesuffix("/test"))
            try:
                with STATE_LOCK:
                    run = run_rule_test(rule_id)
                json_response(self, 200, {"ok": True, "rule_id": rule_id, "run": run})
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

        if parsed.path.startswith("/devices/") and parsed.path.endswith("/status"):
            device_id = clean_device_id(parsed.path.removeprefix("/devices/").removesuffix("/status"))
            try:
                body = read_request_body(self)
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("status body must be a JSON object")
                with STATE_LOCK:
                    device = update_device_status(device_id, payload, self)
                json_response(self, 200, {"ok": True, "device": device})
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
                record_command_result(record)
            json_response(self, 200, device_response)
        except Exception as exc:
            json_response(self, 400, {
                "ok": False,
                "transcript": "",
                "display_text": "Command failed.",
                "tone": "error",
                "error": str(exc),
            })

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/r1-note":
            try:
                payload = read_optional_json_body(self)
                with STATE_LOCK:
                    note = set_r1_note(payload, updated_by="api")
                json_response(self, 200, {"ok": True, "r1_note": note})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return
        if parsed.path.startswith("/firmware/bin/"):
            parts = parsed.path.strip("/").split("/", 4)
            if len(parts) != 5:
                json_response(self, 404, {"error": "expected /firmware/bin/{device_type}/{version}/{filename}"})
                return
            _firmware, _bin, device_type, version, filename = parts
            try:
                body = read_request_body(self, MAX_FIRMWARE_BYTES)
                cleaned_type = clean_device_type(device_type)
                cleaned_version = clean_version(version)
                cleaned_filename = clean_filename(filename)
                path = firmware_binary_path(cleaned_type, cleaned_version, cleaned_filename)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as handle:
                    handle.write(body)
                sha256 = hashlib.sha256(body).hexdigest()
                url = f"/firmware/bin/{cleaned_type}/{cleaned_version}/{cleaned_filename}"
                metadata = {
                    "filename": cleaned_filename,
                    "size": len(body),
                    "sha256": sha256,
                    "url": url,
                    "content_type": self.headers.get("Content-Type", "application/octet-stream"),
                    "notes": self.headers.get("X-Firmware-Notes", ""),
                }
                with STATE_LOCK:
                    record = add_firmware_catalog_entry(cleaned_type, cleaned_version, metadata)
                json_response(self, 200, {"ok": True, "firmware": record})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": str(exc)})
            return
        json_response(self, 404, {"error": "not found"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/rules/"):
            rule_id = clean_rule_id(parsed.path.removeprefix("/rules/"))
            with STATE_LOCK:
                existed = EVENT_RULES.pop(rule_id, None) is not None
                save_event_rules()
            json_response(self, 200, {"ok": True, "rule_id": rule_id, "removed": existed})
            return
        if parsed.path.startswith("/timers/"):
            timer_id = clean_rule_id(parsed.path.removeprefix("/timers/"))
            with STATE_LOCK:
                timer = cancel_timer(timer_id, "")
            json_response(self, 200, {"ok": True, "timer_id": timer_id, "removed": timer is not None, "timer": timer})
            return
        if parsed.path.startswith("/uptime/"):
            monitor_id = clean_rule_id(parsed.path.removeprefix("/uptime/"))
            with STATE_LOCK:
                existed = UPTIME_MONITORS.pop(monitor_id, None) is not None
                save_uptime_monitors()
            json_response(self, 200, {"ok": True, "monitor_id": monitor_id, "removed": existed})
            return
        if parsed.path.startswith("/firmware/catalog/"):
            parts = parsed.path.strip("/").split("/")
            if len(parts) not in (3, 4):
                json_response(self, 404, {"error": "expected /firmware/catalog/{device_type} or /firmware/catalog/{device_type}/{version}"})
                return
            _firmware, _catalog, device_type = parts[:3]
            version = parts[3] if len(parts) == 4 else None
            with STATE_LOCK:
                removed = remove_firmware_catalog_entry(device_type, version)
            json_response(self, 200, {"ok": True, "device_type": clean_device_type(device_type), "version": version, "removed": removed})
            return
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
    init_database()
    load_recent_history()
    load_firmware_catalog()
    load_script_actions()
    load_device_friendly_names()
    load_device_registry()
    load_event_rules()
    load_timers()
    load_uptime_monitors()
    load_mission_tasks()
    load_r1_note()
    threading.Thread(target=timer_worker, name="timer-worker", daemon=True).start()
    threading.Thread(target=uptime_worker, name="uptime-worker", daemon=True).start()
    if RELAY_ENABLED:
        threading.Thread(target=relay_sync_worker, name="relay-sync-worker", daemon=True).start()
    if RELAY_PAIRING_ENABLED:
        threading.Thread(target=relay_pairing_worker, name="relay-pairing-worker", daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), CommandHandler)
    print(f"Listening on http://{HOST}:{PORT}")
    print("POST audio to /audio/command with ELEVENLABS_API_KEY set in the environment.")
    server.serve_forever()


if __name__ == "__main__":
    main()
