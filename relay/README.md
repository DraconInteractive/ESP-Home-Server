# Spoken Command Relay

Small public relay for remote spoken-command devices.

The relay is intentionally narrower than the local command server. It accepts
authenticated remote device registrations and button events, queues those events
for the home server, and serves a read-only dashboard snapshot.

## Layout

`server.py` is a thin entrypoint. The implementation lives in the `relay_app`
package next to it, and the dashboard UI is a static asset:

```text
relay/
  server.py            # entrypoint: main() + server bootstrap
  relay_app/
    config.py          # environment settings and the shared STATE_LOCK
    util.py            # pure helpers (input sanitisers, JSON coercion)
    db.py              # SQLite connection (WAL) and schema init
    auth.py            # tokens, dashboard codes/sessions, ntfy, device-token cache
    http_util.py       # HTTP request/response helpers
    store.py           # device/event/paired/snapshot persistence
    dashboard.py       # loads the dashboard HTML asset
    handler.py         # route dispatch (do_GET / do_POST)
  static/
    dashboard.html     # dashboard UI (HTML + CSS + JS)
```

Running `python3 server.py` from the `relay/` directory puts that directory on
`sys.path`, so `import relay_app` resolves. All three of `server.py`,
`relay_app/`, and `static/` must be deployed together.

## Run Locally

```sh
cp .env.example .env.local
$EDITOR .env.local
set -a
. ./.env.local
set +a
python3 server.py
```

The relay listens on `127.0.0.1:8080` by default. On the VPS, Caddy should be
the only public entry point:

```caddyfile
relay.dracon.au {
    reverse_proxy 127.0.0.1:8080
}
```

## Device Enrollment

Set `RELAY_DEVICE_ENROLL_TOKEN` in `.env.local`. A new device uses that token
once when calling `/register`; the relay generates a per-device secret, stores it
in `device-tokens.json`, and returns it in the registration response.

First registration request:

```http
Authorization: Bearer <device-enrollment-token>
```

First registration response includes:

```json
{
  "ok": true,
  "device_secret": "generated-device-secret"
}
```

The remote device should persist that `device_secret` and use it for later
status, registration updates, and button events:

```http
Authorization: Bearer generated-device-secret
```

The generated token file has this shape:

```json
{
  "devices": {
    "remote-button-01": "long-random-device-secret"
  }
}
```

## Endpoints

Public health:

```text
GET /health
GET /spotify/callback
```

`GET /spotify/callback` is the public Spotify OAuth redirect target. Spotify
passes `code` and `state` query parameters; the relay stores the code in memory
for up to 10 minutes so the R1 can retrieve it once.

Dashboard:

```text
GET /dashboard
GET /dashboard-data
GET /r1-note
POST /mission-board/tasks
POST /mission-board/tasks/{task_id}/complete
```

If `RELAY_DASHBOARD_TOKEN` is set, `/dashboard-data` requires:

```http
Authorization: Bearer <dashboard-token>
```

The dashboard can also issue temporary browser sessions through an 8-digit code
sent to ntfy. Configure:

```sh
RELAY_NTFY_URL=https://ntfy.sh
RELAY_NTFY_TOPIC=your-topic
RELAY_NTFY_TOKEN=
RELAY_NTFY_TITLE=Dracon Relay
RELAY_DASHBOARD_CODE_TTL_SECONDS=300
RELAY_DASHBOARD_CODE_REQUEST_SECONDS=60
RELAY_DASHBOARD_SESSION_SECONDS=43200
```

When enabled, the dashboard login screen can request a phone code, verify it,
and store a temporary session token in browser local storage. The long dashboard
token remains valid as a fallback.

Mission-board posts require dashboard access. They enqueue events for the home
server; the home server remains the canonical owner of task state and publishes
the active board back in the dashboard snapshot.

`GET /r1-note` returns the latest relay copy of the `r1-note` text block. The
relay dashboard shows it read-only; dashboard/API clients cannot set the note
through the relay.

IP pairing lets trusted full computers or servers publish their current
connection details to the relay for authenticated dashboard lookup:

```text
POST /paired-devices/{device_id}
```

Pairing updates require:

```http
Authorization: Bearer <RELAY_IP_PAIRING_TOKEN>
```

Example:

```sh
curl https://relay.dracon.au/paired-devices/home-server \
  -H "Authorization: Bearer $RELAY_IP_PAIRING_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"name":"Home Server","type":"antix","hostname":"DraconAXMini","local_ips":["192.168.4.20"],"ports":["ssh:22","dashboard:8080"],"notes":"Primary local command server"}'
```

The relay infers the public source IP from the HTTPS request when
`external_ip` is not supplied. Paired device details are included only in the
authenticated dashboard data; if `RELAY_IP_PAIRING_TOKEN` is configured, the
dashboard requires either the long dashboard token or a valid phone-code
session.

Remote devices:

```text
POST /devices/{device_id}/register
POST /devices/{device_id}/status
POST /devices/{device_id}/button
```

Home server sync:

```text
POST /sync/dashboard-snapshot
GET  /sync/r1-note
POST /sync/r1-note
GET  /sync/spotify-code
GET  /sync/device-statuses
GET  /sync/events
POST /sync/events/{event_id}/ack
```

`POST /sync/r1-note` is the only relay route that provisions the relay copy of
the note, and it requires `RELAY_SYNC_TOKEN`.

`GET /sync/spotify-code?state=<state>` lets the R1 poll for a Spotify OAuth
code. It returns `{"ok": true, "code": null}` until the callback arrives, then
returns the code once and deletes it from memory.

Sync endpoints require:

```http
Authorization: Bearer <sync-token>
```

## Example Requests

Register a remote button:

```sh
curl https://relay.dracon.au/devices/remote-button-01/register \
  -H 'Authorization: Bearer replace-with-device-enrollment-token' \
  -H 'Content-Type: application/json' \
  --data '{"type":"button","model":"Seeed XIAO ESP32-C3","capabilities":["button"],"status":{"battery_percent":95}}'
```

Update device status without re-registering metadata:

```sh
curl https://relay.dracon.au/devices/remote-button-01/status \
  -H 'Authorization: Bearer generated-device-secret' \
  -H 'Content-Type: application/json' \
  --data '{"status":{"battery_percent":94,"uptime_ms":120000}}'
```

Record a button press:

```sh
curl https://relay.dracon.au/devices/remote-button-01/button \
  -H 'Authorization: Bearer generated-device-secret' \
  -H 'Content-Type: application/json' \
  --data '{"event":"click","button":"D10","gpio":10,"click_count":1}'
```

Poll pending events from the home server:

```sh
curl https://relay.dracon.au/sync/events \
  -H 'Authorization: Bearer replace-with-home-server-sync-token'
```

Poll latest status updates from remote devices:

```sh
curl https://relay.dracon.au/sync/device-statuses \
  -H 'Authorization: Bearer replace-with-home-server-sync-token'
```

Ack an event:

```sh
curl https://relay.dracon.au/sync/events/{event_id}/ack \
  -H 'Authorization: Bearer replace-with-home-server-sync-token' \
  -H 'Content-Type: application/json' \
  --data '{"ok":true}'
```

## State

Runtime state is stored in SQLite:

```text
relay-state.sqlite3
```

The database contains remote devices, queued events, ack status, and the latest
home dashboard snapshot.

Event retention is size-based. By default, `RELAY_MAX_EVENT_ROWS=50000`. When
the events table exceeds that count, the relay deletes acked events oldest-first
until the table is back under the limit. Unacked events are never pruned by this
retention pass.

See `../docs/relay-runbook.md` for runtime file handling, backups, token
rotation, health checks, and recovery notes.

## Updating The VPS From Git

Keep `/opt/spoken-command-relay` as the runtime directory. It contains
`.env.local`, `relay-state.sqlite3`, and `device-tokens.json`, so do not replace
it with a fresh checkout.

Clone this repo somewhere separate on the VPS, for example:

```sh
git clone https://github.com/DraconInteractive/ESP-Home-Server.git ~/ESP-Home-Server
```

After changes are pushed to GitHub, update the relay with:

```sh
cd ~/ESP-Home-Server
sudo ./relay/update-relay-from-git.sh main
```

The updater fetches `origin` as the SSH user that invoked `sudo`, fast-forwards
the checkout, copies `server.py`, the `relay_app/` package, and the `static/`
assets into `/opt/spoken-command-relay` (replacing `relay_app/` wholesale so
removed modules do not linger), byte-compiles `server.py` and the package,
restarts `spoken-command-relay`, and checks the local health endpoint.
