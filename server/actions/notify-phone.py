#!/usr/bin/env python3
"""Send a phone notification through ntfy."""

from __future__ import annotations

import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def clean_message(text: str) -> str:
    text = " ".join(text.strip().split())
    text = re.sub(r"^(run action|execute action|action|run script)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"^(notify phone|send notification|phone notification|notify android)\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^(message|saying|that says|with message)\s+", "", text, flags=re.IGNORECASE)
    return text.strip()


def main() -> int:
    base_url = env("COMMAND_SERVER_NTFY_URL", "https://ntfy.sh").rstrip("/")
    topic = env("COMMAND_SERVER_NTFY_TOPIC")
    title = env("COMMAND_SERVER_NTFY_TITLE", "Spoken Command")
    priority = env("COMMAND_SERVER_NTFY_PRIORITY", "default")
    token = env("COMMAND_SERVER_NTFY_TOKEN")
    message = clean_message(env("SCD_TRANSCRIPT")) or env("COMMAND_SERVER_NTFY_DEFAULT_MESSAGE", "Notification from command server.")

    if not topic:
        raise RuntimeError("COMMAND_SERVER_NTFY_TOPIC is not set")

    url = f"{base_url}/{quote(topic, safe='')}"
    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": env("COMMAND_SERVER_NTFY_TAGS", "bell"),
        "User-Agent": "SpokenCommandServer/0.1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=8) as response:
            response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ntfy returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"ntfy request failed: {exc.reason}") from exc

    print("Notification sent")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Notification failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
