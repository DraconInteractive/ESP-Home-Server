# Relay Runbook

This runbook covers the public relay in `relay/`. The relay is the only
internet-facing part of the spoken-command system; the home command server
communicates with it using outbound HTTPS polling and snapshot uploads.

## Runtime Files

The VPS runtime directory is `/opt/spoken-command-relay`.

Keep these files on the VPS and out of Git:

- `.env.local`
- `relay-state.sqlite3`
- `relay-state.sqlite3-*`
- `device-tokens.json`

The repository `.gitignore` also ignores local development copies of those files
under `relay/`.

## Configuration

Create `/opt/spoken-command-relay/.env.local` from `relay/.env.example` and
replace all placeholder tokens before starting the service.

Use separate long random values for:

- `RELAY_DEVICE_ENROLL_TOKEN`
- `RELAY_SYNC_TOKEN`
- `RELAY_DASHBOARD_TOKEN`
- `RELAY_ADMIN_TOKEN`
- `RELAY_IP_PAIRING_TOKEN`

If phone-code dashboard login is enabled, configure `RELAY_NTFY_TOPIC` and
optionally `RELAY_NTFY_TOKEN`. Treat the ntfy topic as sensitive if it receives
dashboard login codes.

## Deployment

The relay should listen on `127.0.0.1:8080`. Caddy or another reverse proxy is
the public HTTPS entry point.

Install the systemd service after copying the relay app and creating
`.env.local`:

```sh
sudo ./relay/install-relay-service.sh
```

Update the VPS from a clean deployment checkout:

```sh
cd ~/ESP-Home-Server
sudo ./relay/update-relay-from-git.sh main
```

Do not run the updater from a development checkout with local edits; it fetches,
checks out the requested ref, and fast-forwards the working tree before copying
files into `/opt/spoken-command-relay`.

## Health Checks

Local relay health:

```sh
curl -fsS http://127.0.0.1:8080/health
```

Public relay health:

```sh
curl -fsS https://relay.dracon.au/health
```

Check service status:

```sh
sudo systemctl --no-pager --full status spoken-command-relay
```

## Backups

Back up these files before server maintenance, token rotation, or relay updates:

```text
/opt/spoken-command-relay/.env.local
/opt/spoken-command-relay/device-tokens.json
/opt/spoken-command-relay/relay-state.sqlite3
```

For a live SQLite backup, prefer the SQLite backup command over copying a
possibly active database file:

```sh
sqlite3 /opt/spoken-command-relay/relay-state.sqlite3 ".backup '/tmp/relay-state.sqlite3.backup'"
```

Store backups somewhere private. They contain device metadata, queued events,
dashboard snapshots, and secrets.

## Token Rotation

Dashboard, sync, admin, pairing, and enrollment tokens are configured in
`.env.local`. Rotate one role at a time:

1. Update the matching token in `/opt/spoken-command-relay/.env.local`.
2. Update any client that uses that token.
3. Restart the relay service.
4. Verify `/health` and the affected endpoint.

Per-device secrets live in `device-tokens.json`. Rotating a device secret
requires updating the file and reconfiguring that device with the new secret.
Removing a device entry prevents that device from posting status or button
events until it enrolls again or receives a new secret.

## Recovery

If the relay starts but remote devices stop syncing:

- Confirm `RELAY_SYNC_TOKEN` matches `COMMAND_SERVER_RELAY_SYNC_TOKEN`.
- Check `GET /sync/events` from the home server network with the sync token.
- Check the home server relay worker logs for ack or processing failures.
- Confirm the relay database is writable by the `relay` user.

If the dashboard cannot authenticate:

- Confirm `RELAY_DASHBOARD_TOKEN` is set correctly.
- If using phone codes, confirm `RELAY_NTFY_TOPIC` and `RELAY_NTFY_TOKEN`.
- Restarting the relay clears temporary browser sessions and pending phone
  codes; log in again with the long dashboard token or request a new code.

If device enrollment fails:

- Confirm `RELAY_DEVICE_ENROLL_TOKEN` is configured.
- Confirm the device is posting `Authorization: Bearer <enrollment-token>`.
- Check whether the device already has an entry in `device-tokens.json`; an
  existing device should use its per-device secret after enrollment.
