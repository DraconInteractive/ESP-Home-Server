# Spoken Command Device Network

Local command-and-control system for a small network of ESP32 devices. The
main interaction model is voice-first: a handheld voice controller streams
microphone audio to a local server, the server transcribes it, interprets the
command, and sends compact events back to displays, cameras, buttons, and
other devices on the network.

The project is intentionally local-network first. Devices register with the
server, advertise their capabilities, and poll or expose endpoints depending on
their role. Cloud use is currently limited to speech-to-text via ElevenLabs.

## What It Can Do Today

- Stream spoken commands from a Waveshare ESP32-C6 Touch AMOLED voice
  controller to the server.
- Transcribe audio with ElevenLabs Speech to Text.
- Run a small server-side command registry with multi-turn prompts.
- Run packaged server-side actions from an allowlisted script registry.
- Track devices by stable ID and optional friendly name.
- Report firmware project/version/device type from registered devices.
- Maintain a local firmware catalog for future OTA rollout.
- Show registered devices, online/offline state, recent commands, and media
  feeds in a browser dashboard.
- Show startup diagnostics, active timers, and packaged action test controls in
  the browser dashboard.
- Queue events to one device or broadcast events to all known devices.
- Display alerts and broadcast text on display devices.
- Proxy camera and audio endpoints through the server so multiple browser
  clients do not overload a device.
- View camera feeds together in a server-hosted grid.
- Register simple button nodes and log button events.

## Repository Layout

| Path | Purpose |
| --- | --- |
| `server/` | Python HTTP server, dashboard, command registry, device registry, media proxy, and ElevenLabs bridge. |
| `firmware/` | Main Waveshare ESP32-C6 Touch AMOLED voice controller firmware. |
| `display-firmware/` | ESP32-C3 round 240x240 display node with touch support. |
| `camera-firmware/` | M5Stack TimerCamera-X firmware with setup AP, mDNS, `/capture`, and `/stream`. |
| `esp-eye-firmware/` | ESP-EYE v2.1 firmware with camera, microphone, `/capture`, `/video`, `/audio`, and combined `/stream`. |
| `button-node-firmware/` | Seeed XIAO ESP32-C3 button node firmware. |
| `nesso-n1-firmware/` | Arduino Nesso N1 display/input firmware with battery-aware boot recovery. |
| `c6-lcd-147-firmware/` | Waveshare ESP32-C6 1.47 inch LCD display node firmware. |
| `windows-pairing-agent/` | Windows tray app that sends relay IP pairing updates for this PC. |

Each firmware project is a standalone ESP-IDF project with its own README and
local `sdkconfig`. Local Wi-Fi credentials, server URLs, and API keys are not
intended to be committed.

## System Architecture

```text
voice controller
  -> chunked PCM upload
  -> local command server
  -> ElevenLabs speech-to-text
  -> command registry
  -> device events / display responses / media proxy

camera devices
  -> register HTTP media endpoints
  -> server proxies capture, audio, video, and stream routes

display devices
  -> register capabilities
  -> poll /devices/{device_id}/events
  -> render alerts, broadcasts, and camera frames

button devices
  -> register capabilities
  -> POST click events to the server
```

The server listens on `0.0.0.0:8080` by default.

## Server

Start the server from `server/`:

```sh
cp .env.example .env.local
$EDITOR .env.local
set -a
. ./.env.local
set +a
python3 server.py
```

For manual restarts over SSH, use:

```sh
./restart-device-server.sh
```

On this antiX setup, the repo also includes a runit installer so the server can
start after reboot:

```sh
sudo ./install-device-server-service.sh
sudo sv status spoken-command-server
sudo sv restart spoken-command-server
```

Useful server pages:

- `http://<server-ip>:8080/dashboard` - device dashboard.
- `http://<server-ip>:8080/cameras` - camera grid.
- `http://<server-ip>:8080/health` - basic health check.

Important API surfaces:

- `POST /audio/command` receives WAV or raw PCM command audio.
- `GET /firmware/catalog` lists known firmware builds by device type.
- `PUT /firmware/bin/{device_type}/{version}/{filename}` uploads a firmware
  binary into the local catalog.
- `GET /actions` lists allowlisted packaged server actions.
- `POST /actions/{action_name}/run` runs an allowlisted packaged server action.
- `POST /devices/{device_id}/register` records device metadata.
- `GET /devices/{device_id}/events` lets polling devices retrieve queued work.
- `POST /devices/{device_id}/events` queues an event for one device.
- `POST /devices/events` broadcasts an event to known devices.
- `GET /media/{device_id}/{endpoint}` proxies device media endpoints.
- `POST /devices/{device_id}/friendly-name` sets a dashboard/spoken friendly
  name.
- `DELETE /devices/{device_id}` removes a historical device entry.

## Built-In Voice Commands

Current commands include:

- `test` - check that the server path is working.
- `help` - show the available command list.
- `status` - show status for the requesting device.
- `status <device name>` - show status for a named device.
- `list devices` - list known devices one per line.
- `ping` - check known devices and remove unreachable entries.
- `mute` / `unmute` - toggle response tones for the requesting device.
- `mute all` / `unmute all` - toggle response tones globally.
- `broadcast <message>` - send text to all polling display devices.
- `alert` / `show alert` - show an alert locally or across devices depending
  on the phrase.
- `repeat <message>` or `say <message>` - display the spoken suffix.
- `set timer` - starts a multi-turn prompt if no duration was supplied.
- `set timer for 5 minutes notify phone` - sends a phone notification when the
  timer completes.
- `set timer for 5 minutes alert all devices` - alerts polling display devices
  when the timer completes.
- `run action <name>` - runs an allowlisted packaged server-side action.
- `run action send email to <address> subject <subject> message <message>` -
  sends an email through the configured SMTP account after confirmation.
- `run action send test email` - sends the fixed test email action after
  confirmation.
- `run action notify phone message <message>` - sends an Android notification
  through ntfy.
- `show security camera on display one` - target a camera/display interaction
  using registered devices and friendly names.

Example multi-turn command:

```text
User: set a timer
Device: How long should the timer be?
User: 5 minutes
Device: Timer set: 5 min
```

The timer command currently confirms intent and duration; a full timer alarm
workflow is still future work.

## Device Firmware

### Voice Controller

`firmware/` targets the Waveshare ESP32-C6 Touch AMOLED 1.8 board. Confirmed
components include:

- SH8601 AMOLED display
- FT5x06 touch controller
- QMI8658 IMU
- ES8311 microphone/speaker audio
- BOOT interaction button
- AXP2101 PMU power/standby handling

The current interaction is hold-to-record: while the button is held, the
firmware streams mono signed 16-bit PCM at 16 kHz to the server using chunked
HTTP. The display shows upload status, transcript/response text, and a subtle
microphone level indicator.

### Display Nodes

Display firmware projects register as display-capable devices and poll the
server for queued events. Supported behavior includes alert rendering,
broadcast text, and camera-frame display experiments.

Current display projects:

- `display-firmware/` for the ESP32-C3 round 240x240 LCD board.
- `nesso-n1-firmware/` for the Arduino Nesso N1.
- `c6-lcd-147-firmware/` for the Waveshare ESP32-C6-LCD-1.47.

The Nesso N1 firmware also initializes the AW32001 charger and checks the
BQ27220 fuel gauge before enabling LCD/Wi-Fi. If the battery is critically low,
it enters a low-power recovery sleep loop so the cell can charge instead of
repeatedly resetting under startup load. LoRa support is deliberately disabled
until antenna-safe transmit handling is implemented.

### Cameras

`camera-firmware/` targets the M5Stack TimerCamera-X. It supports a first-boot
setup access point, saves Wi-Fi credentials to NVS, advertises itself with
mDNS, registers with the server, and exposes:

- `/capture` for a single JPEG frame
- `/stream` for a slow MJPEG stream

`esp-eye-firmware/` targets the ESP-EYE v2.1 and exposes:

- `/capture` for a single JPEG frame
- `/video` for MJPEG video
- `/audio` for a short WAV microphone capture
- `/stream` for a combined audio/video multipart stream

### Button Nodes

`button-node-firmware/` targets a Seeed XIAO ESP32-C3 with a button on
D10/GPIO10. It registers with the server and posts button events to
`/devices/{device_id}/button`.

## Building Firmware

All firmware projects use ESP-IDF. Typical workflow:

```sh
. /home/demo/esp/esp-idf/export.sh
cd <firmware-project>
idf.py menuconfig
idf.py build
idf.py -p /dev/ttyACM0 flash monitor
```

Target chips vary by project:

- ESP32-C6 projects: `firmware/`, `nesso-n1-firmware/`, `c6-lcd-147-firmware/`
- ESP32-C3 projects: `display-firmware/`, `button-node-firmware/`
- ESP32 projects: `camera-firmware/`, `esp-eye-firmware/`

Use `idf.py set-target <chip>` when creating a fresh build directory or moving
between device families.

Firmware versions use a semantic base plus a one-letter release channel suffix:

- `d` for development builds, for example `0.0.1d`
- `s` for staging builds, for example `0.0.1s`
- `p` for production builds, for example `0.0.1p`

## Configuration And Secrets

The server uses environment variables from `.env.local`. Firmware projects use
ESP-IDF `menuconfig`, which writes local settings into `sdkconfig`. These local
files should contain values such as:

- Wi-Fi SSID and password
- command server URL, usually `http://<server-ip>:8080`
- ElevenLabs API key for the server
- per-device optional settings

Do not commit local credentials or generated build output.

The server persists device names, device registry state, recent command
history, and recent button events locally under `server/`. Runtime database,
PID, log, and credential files are ignored by git.

## Current Limitations

- The command registry is intentionally simple and server-local.
- Timers are parsed and acknowledged but do not yet schedule a real alarm.
- Camera display support is still milestone-based and optimized for slow,
  low-resolution frames.
- Device registry state is local to the server process/filesystem.
- SD card support is deferred on boards where no card has been available for
  validation.
- LoRa on the Nesso N1 is disabled by default for hardware safety.

## Project Status

This is an active hardware bring-up and local automation project. Most devices
in the repository have been validated incrementally on physical hardware, and
the code favors explicit, conservative device support over generic abstractions.
