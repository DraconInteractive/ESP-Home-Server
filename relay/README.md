# Spoken Command Relay

Small public relay for remote spoken-command devices.

The relay is intentionally narrower than the local command server. It accepts
authenticated remote device registrations and button events, queues those events
for the home server, and serves a read-only dashboard snapshot.

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

## Device Tokens

Create `device-tokens.json` on the relay server:

```json
{
  "devices": {
    "remote-button-01": "long-random-device-secret"
  }
}
```

Remote devices authenticate with:

```http
Authorization: Bearer long-random-device-secret
```

## Endpoints

Public health:

```text
GET /health
```

Dashboard:

```text
GET /dashboard
GET /dashboard-data
```

If `RELAY_DASHBOARD_TOKEN` is set, `/dashboard-data` requires:

```http
Authorization: Bearer <dashboard-token>
```

Remote devices:

```text
POST /devices/{device_id}/register
POST /devices/{device_id}/button
```

Home server sync:

```text
POST /sync/dashboard-snapshot
GET  /sync/events
POST /sync/events/{event_id}/ack
```

Sync endpoints require:

```http
Authorization: Bearer <sync-token>
```

## Example Requests

Register a remote button:

```sh
curl https://relay.dracon.au/devices/remote-button-01/register \
  -H 'Authorization: Bearer long-random-device-secret' \
  -H 'Content-Type: application/json' \
  --data '{"type":"button","model":"Seeed XIAO ESP32-C3","capabilities":["button"],"status":{"battery_percent":95}}'
```

Record a button press:

```sh
curl https://relay.dracon.au/devices/remote-button-01/button \
  -H 'Authorization: Bearer long-random-device-secret' \
  -H 'Content-Type: application/json' \
  --data '{"event":"click","button":"D10","gpio":10,"click_count":1}'
```

Poll pending events from the home server:

```sh
curl https://relay.dracon.au/sync/events \
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
