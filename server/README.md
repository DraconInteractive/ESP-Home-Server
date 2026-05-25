# Spoken Command Server

Local HTTP bridge between the ESP32-C6 board and ElevenLabs Speech to Text.

## Run

```sh
cp .env.example .env.local
$EDITOR .env.local
source .env.local
python3 server.py
```

The server listens on `0.0.0.0:8080` by default.

For manual restarts over SSH from the repo root:

```sh
./restart-device-server.sh
```

On antiX/runit systems, install the startup service from the repo root:

```sh
sudo ./install-device-server-service.sh
sudo sv status spoken-command-server
```

Runtime state is stored locally:

- `device-names.json` for friendly names
- `device-registry.json` for known devices
- `server-state.sqlite3` for recent command and button event history
- `firmware-catalog.json` and `firmware-catalog/` for OTA catalog metadata and
  uploaded firmware binaries

Firmware versions use a semantic base plus channel suffix: `d` for development,
`s` for staging, and `p` for production. Example: `0.0.1d`.

Packaged server actions are defined in `actions/actions.json` and scripts live
in `actions/`. Only configured actions can run; scripts are invoked directly
without `shell=True`, with a timeout and minimal environment.

The email action uses SMTP credentials from `.env.local`:

```sh
export COMMAND_SERVER_SMTP_HOST="smtp.example.com"
export COMMAND_SERVER_SMTP_PORT="587"
export COMMAND_SERVER_SMTP_USERNAME="user@example.com"
export COMMAND_SERVER_SMTP_PASSWORD="app-password"
export COMMAND_SERVER_EMAIL_FROM="user@example.com"
export COMMAND_SERVER_EMAIL_TO=""
export COMMAND_SERVER_EMAIL_SUBJECT=""
```

If `COMMAND_SERVER_EMAIL_TO` or `COMMAND_SERVER_EMAIL_SUBJECT` are empty, the
spoken action must provide them. If `COMMAND_SERVER_EMAIL_TO` is set, spoken
recipients are ignored unless `COMMAND_SERVER_EMAIL_ALLOW_FREEFORM_TO=1`.

The ntfy phone notification action uses these values:

```sh
export COMMAND_SERVER_NTFY_URL="https://ntfy.sh"
export COMMAND_SERVER_NTFY_TOPIC="your-topic"
export COMMAND_SERVER_NTFY_TITLE="Spoken Command"
```

## Endpoints

- `GET /` or `GET /dashboard` opens the local device dashboard.
- `GET /cameras` or `GET /camera-grid` opens a camera feed grid using proxied
  device media endpoints.
- `GET /dashboard-data` returns the dashboard snapshot used by the UI.
  The dashboard includes startup diagnostics, active timers, and packaged
  action test controls.
- `GET /health` returns a basic health check.
- `GET /actions` returns the configured packaged server actions.
- `POST /actions/{action_name}/run` runs a configured action. If an action is
  marked as confirmation-required, the JSON body must include
  `{"confirm": true}`.
- `GET /firmware/catalog` returns the known firmware catalog summary.
- `GET /firmware/catalog/{device_type}` returns all known firmware versions for
  one device type.
- `POST /firmware/catalog` adds metadata for a firmware version without
  uploading a binary. Example:

```sh
curl -X POST http://127.0.0.1:8080/firmware/catalog \
  -H 'Content-Type: application/json' \
  --data '{"device_type":"arduino-nesso-n1","version":"2026.05.25-1","url":"/firmware/bin/arduino-nesso-n1/2026.05.25-1/nesso_n1_firmware.bin","sha256":"...","size":1089216}'
```

- `PUT /firmware/bin/{device_type}/{version}/{filename}` uploads a firmware
  binary and automatically creates/updates the catalog entry. Example:

```sh
curl -X PUT \
  --data-binary @../nesso-n1-firmware/build/nesso_n1_firmware.bin \
  http://127.0.0.1:8080/firmware/bin/arduino-nesso-n1/2026.05.25-1/nesso_n1_firmware.bin
```

- `GET /firmware/bin/{device_type}/{version}/{filename}` downloads a cataloged
  firmware binary.
- `GET /media/{device_id}/{endpoint}` proxies a registered device media
  endpoint. Supported endpoint names are `capture`, `audio`, `video`, and
  `stream`. Finite endpoints are cached briefly; stream endpoints share one
  upstream device connection across multiple server clients.
- `GET /devices` returns known devices and their server-side state.
- `GET /devices/{device_id}` returns one known device.
- `DELETE /devices/{device_id}` removes a device from the local historical
  registry, including any friendly name. If it registers again later it will
  reappear.
- `POST /devices/{device_id}/friendly-name` sets or clears the local friendly
  name used by the dashboard and spoken target matching. Example:

```sh
curl -X POST http://127.0.0.1:8080/devices/waveshare-c6-fdda98/friendly-name \
  -H 'Content-Type: application/json' \
  --data '{"friendly_name":"Display One"}'
```

- `GET /devices/{device_id}/events` returns and clears the next queued device
  event.
- `POST /devices/events` queues an event for all currently known devices.
  Example:

```sh
curl -X POST http://127.0.0.1:8080/devices/events \
  -H 'Content-Type: application/json' \
  --data '{"type":"alert","display_text":"Alert","tone":"alert"}'
```

- `POST /devices/{device_id}/events` queues an event for a device. Example:

```sh
curl -X POST http://127.0.0.1:8080/devices/waveshare-c6-fde0e0/events \
  -H 'Content-Type: application/json' \
  --data '{"type":"alert","display_text":"Alert","tone":"alert"}'
```

- `POST /devices/{device_id}/register` records device metadata for devices
  that do not post audio or poll events. Example camera registration:

```sh
curl -X POST http://127.0.0.1:8080/devices/timercam-x-a1b2c3/register \
  -H 'Content-Type: application/json' \
  --data '{"type":"camera","model":"M5Stack TimerCamera-X","capabilities":["capture","stream"],"endpoints":{"root":"http://192.168.4.50/","capture":"http://192.168.4.50/capture","stream":"http://192.168.4.50/stream"},"status":{"ip":"192.168.4.50"}}'
```

- `POST /devices/{device_id}/button` records a button event from a simple
  input node. Example:

```sh
curl -X POST http://127.0.0.1:8080/devices/xiao-button-a1b2c3/button \
  -H 'Content-Type: application/json' \
  --data '{"event":"click","button":"D10","gpio":10,"click_count":1}'
```

- `GET /button-events/recent` returns recent button events kept in memory. Add
  `?device_id=xiao-button-a1b2c3` to filter by device.
- `GET /commands/recent` returns recent transcripts kept in memory. Add
  `?device_id=waveshare-c6-fdda98` to filter by device.
- `POST /audio/command` accepts either WAV or raw PCM audio. Raw PCM may use a
  normal `Content-Length` request or HTTP chunked transfer. It returns a compact
  device response:

```json
{
  "ok": true,
  "transcript": "test",
  "command": "test",
  "display_text": "Ready.",
  "tone": "success",
  "state": {
    "muted": false
  }
}
```

For raw PCM, send:

```text
Content-Type: application/octet-stream
X-Audio-Sample-Rate: 16000
X-Audio-Channels: 1
X-Device-Id: waveshare-c6-fdda98
```

Firmware derives `X-Device-Id` from the board Wi-Fi MAC suffix, so each physical
board has separate mute state, pending prompts, and command history.

The current ElevenLabs STT call uses `POST https://api.elevenlabs.io/v1/speech-to-text`,
`model_id=scribe_v2`, and a multipart `file` field.

## Built-in Commands

- `test` returns `Ready.`
- `status` returns `Server online.`
- `list devices` returns known devices and IP addresses.
- `ping` checks known devices and removes offline entries.
- `broadcast <message>` sends a silent alert-style event to polling devices.
- `run action <name>` runs an allowlisted packaged server-side action.
- `run action send email to <address> subject <subject> message <message>`
  sends an email through the configured SMTP account after asking for
  confirmation.
- `run action notify phone message <message>` sends an Android notification
  through ntfy.
- `help` returns a short command list.
- `cancel` cancels a pending command prompt.
- `mute` disables response tones for that device.
- `unmute` enables response tones for that device.
- `mute all` disables response tones globally.
- `unmute all` clears global and per-device mute state.
- `repeat ...` or `say ...` displays the spoken suffix.
- `timer`, `set timer`, or `set a timer` asks for a duration if one was not
  supplied. The next response from the same device completes the timer.
- `set timer for 5 minutes notify phone` sends an ntfy phone notification when
  the timer completes.
- `set timer for 5 minutes alert all devices` queues alert events on polling
  display devices when the timer completes.
- `alert`, `show alert`, or `show an alert on all devices` queues an alert event
  for the current device or all known devices.
- Anything else displays `Heard: ...`.

Example timer exchange:

```text
User: set a timer
Device: How long should the timer be?
User: 5 minutes
Device: Timer set: 5 min
```

Example timer with notification:

```text
User: set timer for 5 minutes notify phone
Device: Timer set: 5 min -> phone notification
```

Example packaged action command:

```text
User: run action server time
Device: 2026-05-25 14:30:00 AWST
```

Example email action command:

```text
User: run action send email to recipient@example.com subject test message the test message worked
Device: Confirm action: send email?
User: confirm
Device: Email sent to recipient@example.com
```

Example fixed test email action:

```text
User: run action send test email
Device: Confirm action: send test email?
User: confirm
Device: Email sent to rarceth@gmail.com
```

Example phone notification action:

```text
User: run action notify phone message this is a test notification
Device: Notification sent
```
