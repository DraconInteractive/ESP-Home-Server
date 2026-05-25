# Waveshare ESP32-C6-LCD-1.47 firmware

ESP-IDF firmware for the Waveshare ESP32-C6-LCD-1.47 display node.

Initial milestone:

- bring up the 172x320 ST7789 LCD
- connect to Wi-Fi
- register with the spoken command server as a display device
- poll for server events and render alert/broadcast text

Known board pins from Waveshare documentation:

- LCD MOSI: GPIO6
- LCD SCLK: GPIO7
- LCD CS: GPIO14
- LCD DC: GPIO15
- LCD RST: GPIO21
- LCD BL: GPIO22
- RGB LED: GPIO8
- TF card MISO: GPIO5
- TF card CS: GPIO4

Configure:

```sh
. /home/demo/esp/esp-idf/export.sh
idf.py menuconfig
```

Build:

```sh
. /home/demo/esp/esp-idf/export.sh
idf.py build
```

Flash:

```sh
. /home/demo/esp/esp-idf/export.sh
idf.py -p /dev/ttyACM0 flash monitor
```
