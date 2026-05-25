# Arduino Nesso N1 firmware

ESP-IDF firmware for the Arduino Nesso N1, an ESP32-C6 device with a
135x240 ST7789P3 LCD, FT6336U touch controller, BMI270 IMU, BQ27220 fuel
gauge, buzzer, IR LED, Grove/Qwiic connectors, and an SX1262 LoRa
transceiver.

Initial milestone:

- bring up the 1.14" ST7789P3 LCD
- connect to Wi-Fi
- register with the spoken command server as a display-capable device
- poll for server events and render alert/broadcast text

LoRa safety:

- The SX1262 is intentionally disabled by default.
- Do not enable LoRa transmit paths unless the detachable antenna is attached.
- Current firmware only records `"lora-disabled"` as a server capability and
  keeps the LoRa front-end expander pins inactive.

Confirmed pin map:

- LCD MOSI: GPIO21
- LCD SCLK: GPIO20
- LCD CS: GPIO17
- LCD RS/DC: GPIO16
- LCD reset: PI4IOE5V6408 at 0x44, E1.P1
- LCD backlight: PI4IOE5V6408 at 0x44, E1.P6
- Touch FT6336U: I2C GPIO10 SDA, GPIO8 SCL, GPIO3 INT
- IMU BMI270: I2C GPIO10 SDA, GPIO8 SCL, GPIO3 INT
- Battery fuel gauge BQ27220YZFR: I2C GPIO10 SDA, GPIO8 SCL
- Buzzer: GPIO11
- IR transmitter: GPIO9
- Qwiic: GPIO10 SDA, GPIO8 SCL
- Grove: GPIO5, GPIO4
- LoRa SX1262: GPIO21 MOSI, GPIO22 MISO, GPIO20 SCK, GPIO23 CS, GPIO19 BUSY, GPIO15 IRQ
- LoRa front-end control: PI4IOE5V6408 at 0x43, E0.P5/E0.P6/E0.P7
- User buttons: PI4IOE5V6408 at 0x43, E0.P0/E0.P1
- Green LED: PI4IOE5V6408 at 0x44, E1.P7

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
