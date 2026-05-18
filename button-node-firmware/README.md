# XIAO ESP32-C3 Button Node

ESP-IDF firmware for a Seeed XIAO ESP32-C3 with a button on D10/GPIO10.

The node:

- connects to Wi-Fi,
- registers with the command server as `xiao-button-<mac-suffix>`,
- sends `POST /devices/{device_id}/button` when the button is clicked.

Configure Wi-Fi and server URL with:

```bash
idf.py menuconfig
```

Relevant config:

- `BUTTON_NODE_WIFI_SSID`
- `BUTTON_NODE_WIFI_PASSWORD`
- `BUTTON_NODE_SERVER_URL`
- `BUTTON_NODE_BUTTON_GPIO`, default `10`
- `BUTTON_NODE_ACTIVE_LOW`, default enabled

Build and flash:

```bash
idf.py build
idf.py -p /dev/ttyACM0 flash monitor
```
