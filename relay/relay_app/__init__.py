"""Application package for the public spoken-command relay.

The relay exposes a small surface: remote devices register and enqueue events,
and the home server polls those events over an authenticated outbound
connection. Modules:

- ``config``    -- environment-derived settings and shared locks
- ``util``      -- small pure helpers (sanitisers, JSON coercion)
- ``db``        -- SQLite connection and schema management
- ``auth``      -- tokens, dashboard codes/sessions, ntfy, device-token cache
- ``http_util`` -- HTTP request/response helpers
- ``store``     -- device/event/paired/snapshot persistence and snapshots
- ``dashboard`` -- loads the dashboard HTML asset
- ``handler``   -- the ``BaseHTTPRequestHandler`` routing table
"""
