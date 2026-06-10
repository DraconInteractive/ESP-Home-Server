idf_component_register(SRCS "main.c"
                    INCLUDE_DIRS "."
                    REQUIRES driver esp-tls esp_event esp_http_client esp_lcd esp_netif esp_timer esp_wifi nvs_flash)
