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
POST /devices/{device_id}/status
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
the checkout, copies the relay app files into `/opt/spoken-command-relay`,
compiles `server.py`, restarts `spoken-command-relay`, and checks the local
health endpoint.
