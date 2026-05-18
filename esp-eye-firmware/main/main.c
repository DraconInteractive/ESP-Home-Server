#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "esp_check.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_psram.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

static const char *TAG = "esp_eye";

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT BIT1
#define WIFI_MAX_RETRIES 5

static EventGroupHandle_t s_wifi_event_group;
static int s_wifi_retry_count;
static bool s_wifi_ready;
static char s_device_id[48] = "esp-eye-unknown";
static char s_ip_addr[16] = "0.0.0.0";

static void init_device_id(void)
{
    uint8_t mac[6] = {0};
    ESP_ERROR_CHECK(esp_read_mac(mac, ESP_MAC_WIFI_STA));
    snprintf(s_device_id, sizeof(s_device_id), "esp-eye-%02x%02x%02x", mac[3], mac[4], mac[5]);
    ESP_LOGI(TAG, "Device ID: %s", s_device_id);
}

static esp_err_t post_json(const char *path, const char *body, int *status_out)
{
    char url[240] = {0};
    snprintf(url, sizeof(url), "%s%s", CONFIG_ESP_EYE_COMMAND_SERVER_URL, path);

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 2500,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    ESP_RETURN_ON_FALSE(client != NULL, ESP_ERR_NO_MEM, TAG, "create HTTP client");

    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_header(client, "X-Device-Id", s_device_id);
    esp_http_client_set_post_field(client, body, strlen(body));
    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);

    if (status_out) {
        *status_out = status;
    }
    return err;
}

static void register_with_server(void)
{
    if (!s_wifi_ready || strlen(CONFIG_ESP_EYE_COMMAND_SERVER_URL) == 0) {
        return;
    }

    char path[96] = {0};
    char body[512] = {0};
    int status = 0;
    bool psram_ready = esp_psram_is_initialized();

    snprintf(path, sizeof(path), "/devices/%s/register", s_device_id);
    snprintf(body, sizeof(body),
             "{"
             "\"type\":\"sensor\","
             "\"model\":\"ESP-EYE v2.1\","
             "\"capabilities\":[\"camera\",\"microphone\"],"
             "\"status\":{"
             "\"ip\":\"%s\","
             "\"camera\":\"OV2640\","
             "\"microphone\":\"MEMS\","
             "\"psram_ready\":%s,"
             "\"flash_mb\":4,"
             "\"stage\":\"registration\""
             "}"
             "}",
             s_ip_addr,
             psram_ready ? "true" : "false");

    esp_err_t err = post_json(path, body, &status);
    if (err == ESP_OK && status >= 200 && status < 300) {
        ESP_LOGI(TAG, "Registered with command server");
    } else {
        ESP_LOGW(TAG, "Registration failed: err=%s status=%d", esp_err_to_name(err), status);
    }
}

static void registration_task(void *arg)
{
    while (true) {
        register_with_server();
        vTaskDelay(pdMS_TO_TICKS(CONFIG_ESP_EYE_REGISTER_INTERVAL_SECONDS * 1000));
    }
}

static void wifi_event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        s_wifi_ready = false;
        if (s_wifi_retry_count < WIFI_MAX_RETRIES) {
            s_wifi_retry_count++;
            esp_wifi_connect();
        } else {
            xEventGroupSetBits(s_wifi_event_group, WIFI_FAIL_BIT);
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *event = (const ip_event_got_ip_t *)event_data;
        s_wifi_retry_count = 0;
        s_wifi_ready = true;
        snprintf(s_ip_addr, sizeof(s_ip_addr), IPSTR, IP2STR(&event->ip_info.ip));
        ESP_LOGI(TAG, "Wi-Fi connected ip=%s", s_ip_addr);
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static esp_err_t wifi_init_sta(void)
{
    ESP_RETURN_ON_FALSE(strlen(CONFIG_ESP_EYE_WIFI_SSID) > 0, ESP_ERR_INVALID_STATE, TAG, "Wi-Fi SSID is empty");

    s_wifi_event_group = xEventGroupCreate();
    ESP_RETURN_ON_FALSE(s_wifi_event_group != NULL, ESP_ERR_NO_MEM, TAG, "create Wi-Fi event group");
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_config));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL, NULL));

    wifi_config_t wifi_config = {0};
    strlcpy((char *)wifi_config.sta.ssid, CONFIG_ESP_EYE_WIFI_SSID, sizeof(wifi_config.sta.ssid));
    strlcpy((char *)wifi_config.sta.password, CONFIG_ESP_EYE_WIFI_PASSWORD, sizeof(wifi_config.sta.password));
    wifi_config.sta.threshold.authmode = strlen(CONFIG_ESP_EYE_WIFI_PASSWORD) > 0 ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT, pdFALSE, pdFALSE, pdMS_TO_TICKS(10000));
    return s_wifi_ready ? ESP_OK : ESP_FAIL;
}

void app_main(void)
{
    esp_err_t nvs_err = nvs_flash_init();
    if (nvs_err == ESP_ERR_NVS_NO_FREE_PAGES || nvs_err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        nvs_err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(nvs_err);

    init_device_id();

    if (wifi_init_sta() != ESP_OK) {
        ESP_LOGW(TAG, "Wi-Fi not connected; registration will retry after restart");
    }

    register_with_server();
    xTaskCreate(registration_task, "registration", 4096, NULL, 4, NULL);
}
