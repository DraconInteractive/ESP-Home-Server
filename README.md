# Spoken Command Device

Two-project workspace for the Waveshare ESP32-C6 Touch AMOLED board and a local
server that forwards spoken commands to transcription.

## Projects

- `firmware/`: ESP-IDF project copied from the validated hardware bring-up.
  Current baseline includes display, touch, IMU, microphone input, BOOT
  interaction button, AXP2101 PWR standby handling, streamed audio upload, and
  transcript display.
- `server/`: Python local HTTP server that receives audio from the board and
  forwards it to ElevenLabs Speech to Text.
- `button-node-firmware/`: ESP-IDF project for a Seeed XIAO ESP32-C3 button
  node. It registers with the server and posts click events from D10/GPIO10.

## Current Flow

1. Hold `BOOT` to record a spoken command.
2. Firmware streams mono signed 16-bit PCM at 16 kHz to
   `http://<server-ip>:8080/audio/command` using chunked HTTP.
3. The local server assembles the chunks into WAV and forwards the audio to
   ElevenLabs Speech to Text.
4. The board displays the returned transcript on the AMOLED.

The SD card reader is intentionally deferred until a card is available.
