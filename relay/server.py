#!/usr/bin/env python3
"""Public relay for remote spoken-command devices.

The relay intentionally exposes a much smaller surface than the local command
server. Remote devices can register and enqueue events, while the home server
polls those events using an outbound authenticated connection.

This file is a thin entrypoint; the implementation lives in the ``relay_app``
package next to it (config, db, auth, http_util, store, dashboard, handler).
"""

from __future__ import annotations

from http.server import ThreadingHTTPServer

from relay_app import config
from relay_app.db import init_database
from relay_app.handler import RelayHandler


def main() -> None:
    init_database()
    if not config.DEVICE_ENROLL_TOKEN:
        print("WARNING: RELAY_DEVICE_ENROLL_TOKEN is not configured; new device enrollment will reject requests.")
    if not config.SYNC_TOKEN:
        print("WARNING: RELAY_SYNC_TOKEN is not configured; sync endpoints will reject requests.")
    if not config.DASHBOARD_TOKEN:
        print("WARNING: RELAY_DASHBOARD_TOKEN is not configured; dashboard data is public.")
    print(f"Starting spoken-command relay on {config.HOST}:{config.PORT}")
    server = ThreadingHTTPServer((config.HOST, config.PORT), RelayHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
