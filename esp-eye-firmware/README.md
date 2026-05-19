# ESP-EYE v2.1 Firmware

ESP-IDF firmware for an ESP-EYE v2.1 board.

Current milestone:

- connect to Wi-Fi,
- register with the command server as `esp-eye-<mac-suffix>`,
- initialize the OV2640 camera,
- host a local HTTP camera page,
- expose `/capture` for a single JPEG frame,
- expose `/stream` for a slow MJPEG stream,
- advertise camera endpoints and microphone capability,
- refresh registration periodically.

Configure Wi-Fi and server URL with:

```bash
idf.py menuconfig
```

Relevant config:

- `ESP_EYE_WIFI_SSID`
- `ESP_EYE_WIFI_PASSWORD`
- `ESP_EYE_COMMAND_SERVER_URL`
- `ESP_EYE_REGISTER_INTERVAL_SECONDS`

Build and flash:

```bash
idf.py set-target esp32
idf.py build
idf.py -p /dev/ttyUSB0 flash monitor
```

After boot, the firmware logs the device URL:

- `http://<device-ip>/`
- `http://<device-ip>/capture`
- `http://<device-ip>/stream`

The same endpoints are also sent to the command server during registration.
