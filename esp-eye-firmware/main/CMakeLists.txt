idf_component_register(SRCS "main.c"
  INCLUDE_DIRS "."
  REQUIRES esp_http_server esp_http_client esp_wifi mdns nvs_flash esp_timer esp_psram esp32-camera
)
