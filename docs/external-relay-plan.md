# External Relay Plan

This note captures the relay architecture for exposing a limited external view
of the spoken-command/device network without exposing the local command server
directly. The first relay implementation now exists in `relay/`; this document
keeps the original design intent and records the current implemented behavior.

## Goal

- Provide an external dashboard similar to the current local dashboard, but
  read-only and with reduced functionality.
- Allow devices outside the home network to register and send events over the
  internet.
- Keep the home AntiX command server private, with no router port forwarding and
  no direct public access to the local dashboard/API.

## Recommended Architecture

```text
Remote browser / remote ESP device
        |
        | HTTPS + authentication
        v
Public relay server
        |
        | outbound authenticated sync/polling
        v
Home AntiX command server
        |
        v
Local ESP devices
```

The relay is the only internet-facing service. The home server talks to the
relay using outbound requests, so the home network does not need inbound ports
opened.

## External Dashboard Scope

The public dashboard is intentionally narrower than the local dashboard. It can
show:

- device list
- online/offline state
- friendly names
- firmware versions
- capabilities
- last seen
- battery state
- recent events/logs
- mission-board state published by the home server
- paired trusted computer/server connection details, after dashboard auth

It must not expose:

- simulated transcripts
- shell/server actions
- broadcast
- timer creation/cancellation
- firmware upload
- arbitrary device event injection
- unrestricted media proxying

The relay dashboard may enqueue mission-board task creates/completions. The home
server remains the canonical owner of task state and publishes the resulting
board back to the relay snapshot. Camera thumbnails or streams can be considered
later, but should be treated as a separate security review.

## Remote Device Registration

Remote devices should register directly with the relay:

```text
POST https://relay.example.com/devices/{device_id}/register
POST https://relay.example.com/devices/{device_id}/status
POST https://relay.example.com/devices/{device_id}/button
```

The relay stores registration and button events. The home command server then
consumes them and feeds them into the existing local device and event-rule
system. Status updates are stored on the relay and synced separately through the
dirty-status endpoint.

Initial event flow:

```text
Remote button pressed
  -> relay stores button event
  -> home server polls relay
  -> home server receives event
  -> existing Event Rules execute locally
  -> dashboard state updates
```

Polling is the recommended first implementation because it is simpler and more
robust. A persistent WebSocket from the home server to the relay can be added
later if lower latency is required.

## Authentication Model

Use separate credentials for each role:

- dashboard/admin auth for browser access
- home-server sync token
- IP-pairing token for trusted computers/servers
- one-time device enrollment token
- per-device token for remote ESP devices

Each remote device has its own long random secret:

```json
{
  "device_id": "remote-button-01",
  "device_secret": "long-random-secret"
}
```

Device auth currently uses:

```http
Authorization: Bearer <device_secret>
```

Dashboard auth supports either a long dashboard token or temporary browser
sessions created by an 8-digit ntfy phone code. The phone-code path is a
convenience login, not a replacement for protecting the relay and ntfy topic.

Later hardening can move to request signing with HMAC over the request body and
timestamp.

## Hosting Options

### Option A: Small VPS

Recommended first choice.

Use a low-cost VPS from a provider such as DigitalOcean, Hetzner, Vultr, or
Linode. DigitalOcean basic droplets currently start around USD $4/month. Hetzner
can be cheaper or more capable depending on region, though its pricing has moved
recently.

Suggested stack:

```text
VPS
  Caddy or nginx
  relay/server.py
  SQLite
  HTTPS
  device tokens
  dashboard login
```

Pros:

- simple to build and debug
- works well with ESP32 HTTPS clients
- easy SQLite persistence
- clear logs
- full control over auth and relay behavior
- can later add WebSockets

Cons:

- expected cost around USD $4-8/month
- VPS must be patched and secured
- SSH access must be locked down

Security baseline:

- only ports 80/443 public
- SSH key auth only
- firewall enabled
- automatic security updates if practical
- Caddy-managed HTTPS or nginx plus Certbot
- relay process runs as a restricted user
- rate limits on device/event endpoints
- audit logging for all remote events

### Option B: Cloudflare Worker + D1/KV

Likely free at this project's scale and avoids maintaining a Linux server.

Pros:

- no VPS maintenance
- strong edge-hosted HTTPS entry point
- suitable for read-only dashboard and event queue
- likely free for low request volume

Cons:

- relay would likely be JavaScript/TypeScript instead of Python
- less direct fit with the current codebase
- persistent connections/WebSockets are more complex
- debugging device interactions may be less straightforward

### Option C: Cloudflare Tunnel to Home Server

Useful for private or protected access, but not ideal as the primary architecture
for remote ESP devices.

Cloudflare Tunnel can protect the local dashboard with Access, but requests still
terminate against the local application. This is acceptable for admin access if
carefully restricted, but a custom relay is safer for internet-facing device
registration.

### Option D: Tailscale

Excellent for private admin access from a phone/laptop. Not suitable for ESP32
devices directly, because they cannot practically join a Tailscale network.

## Recommendation

Use a small VPS for the first relay.

Reasons:

- lowest implementation friction
- easiest to keep in Python
- easiest to debug
- good match for remote ESP device registration
- clear separation between public relay and private home server

Estimated running cost: USD $4-8/month.

## First Milestone

Create a `relay/` project in this repo, but do not expose the local server
directly.

Initial relay endpoints:

```text
GET  /dashboard
GET  /dashboard-data
POST /devices/{device_id}/register
POST /devices/{device_id}/status
POST /devices/{device_id}/button
POST /sync/dashboard-snapshot
GET  /sync/device-statuses
GET  /sync/events
POST /sync/events/{event_id}/ack
```

Initial local-server change:

- add a sync worker that polls the relay for pending remote events
- convert remote relay events into the existing local event-rule flow

The first remote-device test target should be one of the XIAO button devices.

## Implemented Notes

- The relay app lives in `relay/`.
- The relay is deployed behind Caddy at `https://relay.dracon.au`.
- Remote device registration uses a one-time enrollment token to generate and
  persist a per-device secret.
- The local server relay worker is controlled by `COMMAND_SERVER_RELAY_ENABLED`,
  `COMMAND_SERVER_RELAY_URL`, and `COMMAND_SERVER_RELAY_SYNC_TOKEN`.
- The local server pushes a reduced dashboard snapshot and polls/acks remote
  `register`, `status`, `button`, and mission-board events.
- The relay stores runtime state in SQLite and device secrets in
  `device-tokens.json`.
- The dashboard supports temporary ntfy phone-code sessions when configured.
- IP pairing lets trusted full computers or servers publish their current
  external IP, local IPs, ports, hostname, and notes for authenticated dashboard
  lookup.

## Remaining Hardening

- Add relay-side rate limits for registration, status, button, dashboard auth,
  and mission-board mutation endpoints.
- Add a documented token-rotation process for dashboard, sync, pairing,
  enrollment, and per-device secrets.
- Consider storing per-device token hashes instead of raw bearer tokens.
- Keep audit logs or summaries for public device events and dashboard mutations.
- Keep media proxying and camera streams out of the relay until they have a
  separate security review.
