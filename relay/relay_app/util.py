"""Small pure helpers shared across modules. No internal dependencies."""

from __future__ import annotations

import json
import re
from typing import Any


def clean_id(value: str, fallback: str = "") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", value.strip())
    return cleaned[:80] or fallback


def clean_filename(value: str, fallback: str = "file.bin") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().split("/")[-1])
    return cleaned[:120] or fallback


def clean_sha256(value: str) -> str:
    cleaned = str(value).strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", cleaned):
        return cleaned
    return ""


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


def clean_mission_task_type(value: str) -> str:
    cleaned = str(value).strip().lower()
    if cleaned in {"daily", "today"}:
        return "daily"
    return "persistent"


def json_obj(value: str | None) -> dict[str, Any]:
    """Parse a stored JSON column into a dict, tolerating bad/empty data."""
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
