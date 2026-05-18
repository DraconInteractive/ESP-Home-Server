# ESP-EYE v2.1 Firmware

ESP-IDF firmware for an ESP-EYE v2.1 board.

Current milestone:

- connect to Wi-Fi,
- register with the command server as `esp-eye-<mac-suffix>`,
- advertise camera and microphone capabilities for later work,
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
