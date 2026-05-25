#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_camera.h"
#include "esp_check.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_psram.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "mdns.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "sdkconfig.h"

#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"

static const char *TAG = "timercam";
#define FIRMWARE_PROJECT "camera-firmware"
#define FIRMWARE_VERSION "0.0.1d"
#define FIRMWARE_DEVICE_TYPE "timercam-x"

// M5Stack TimerCamera-X / ESP32-D0WDQ6-V3 + OV3660 pin map.
// Source: M5Stack TimerCamera-X docs.
#define CAM_PIN_PWDN    -1
#define CAM_PIN_RESET   15
#define CAM_PIN_XCLK    27
#define CAM_PIN_SIOD    25
#define CAM_PIN_SIOC    23
#define CAM_PIN_D7      19
#define CAM_PIN_D6      36
#define CAM_PIN_D5      18
#define CAM_PIN_D4      39
#define CAM_PIN_D3       5
#define CAM_PIN_D2      34
#define CAM_PIN_D1      35
#define CAM_PIN_D0      32
#define CAM_PIN_VSYNC   22
#define CAM_PIN_HREF    26
#define CAM_PIN_PCLK    21
#define LED_PIN          2
#define BAT_HOLD_PIN    33

#define SETUP_AP_SSID "TimerCamera-X-Setup"
#define SETUP_AP_PASS "timercam123"
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1
#define REGISTRATION_INTERVAL_MS 30000

static EventGroupHandle_t wifi_events;
static httpd_handle_t server;
static char current_ip[16] = "0.0.0.0";
static char device_id[48] = "timercam-x-unknown";

typedef struct {
    char ssid[33];
    char password[65];
} wifi_creds_t;

static esp_err_t creds_load(wifi_creds_t *creds)
{
    memset(creds, 0, sizeof(*creds));
    nvs_handle_t nvs;
    esp_err_t err = nvs_open("wifi", NVS_READONLY, &nvs);
    if (err != ESP_OK) {
        return err;
    }

    size_t ssid_len = sizeof(creds->ssid);
    size_t pass_len = sizeof(creds->password);
    err = nvs_get_str(nvs, "ssid", creds->ssid, &ssid_len);
    if (err == ESP_OK) {
        err = nvs_get_str(nvs, "password", creds->password, &pass_len);
    }
    nvs_close(nvs);

    if (err == ESP_OK && creds->ssid[0] != '\0') {
        return ESP_OK;
    }
    return ESP_ERR_NOT_FOUND;
}

static esp_err_t creds_save(const char *ssid, const char *password)
{
    nvs_handle_t nvs;
    ESP_RETURN_ON_ERROR(nvs_open("wifi", NVS_READWRITE, &nvs), TAG, "open wifi nvs");
    ESP_RETURN_ON_ERROR(nvs_set_str(nvs, "ssid", ssid), TAG, "save ssid");
    ESP_RETURN_ON_ERROR(nvs_set_str(nvs, "password", password), TAG, "save password");
    ESP_RETURN_ON_ERROR(nvs_commit(nvs), TAG, "commit wifi nvs");
    nvs_close(nvs);
    return ESP_OK;
}

static void creds_clear(void)
{
    nvs_handle_t nvs;
    if (nvs_open("wifi", NVS_READWRITE, &nvs) == ESP_OK) {
        nvs_erase_all(nvs);
        nvs_commit(nvs);
        nvs_close(nvs);
    }
}

static char from_hex(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return 0;
}

static void url_decode(char *dst, size_t dst_len, const char *src)
{
    size_t di = 0;
    for (size_t si = 0; src[si] != '\0' && di + 1 < dst_len; ++si) {
        if (src[si] == '%' && src[si + 1] && src[si + 2]) {
            dst[di++] = (char)((from_hex(src[si + 1]) << 4) | from_hex(src[si + 2]));
            si += 2;
        } else if (src[si] == '+') {
            dst[di++] = ' ';
        } else {
            dst[di++] = src[si];
        }
    }
    dst[di] = '\0';
}

static void init_device_id(void)
{
    uint8_t mac[6] = {0};
    ESP_ERROR_CHECK(esp_read_mac(mac, ESP_MAC_WIFI_STA));
    snprintf(device_id, sizeof(device_id), "timercam-x-%02x%02x%02x", mac[3], mac[4], mac[5]);
    ESP_LOGI(TAG, "Device ID: %s", device_id);
}

static void register_with_command_server(void)
{
    if (strcmp(current_ip, "0.0.0.0") == 0 || strlen(CONFIG_TIMERCAM_COMMAND_SERVER_URL) == 0) {
        return;
    }

    char url[192] = {0};
    char body[900] = {0};
    snprintf(url, sizeof(url), "%s/devices/%s/register", CONFIG_TIMERCAM_COMMAND_SERVER_URL, device_id);
    snprintf(body, sizeof(body),
             "{"
             "\"type\":\"camera\","
             "\"device_type\":\"" FIRMWARE_DEVICE_TYPE "\","
             "\"model\":\"M5Stack TimerCamera-X\","
             "\"firmware\":{"
             "\"project\":\"" FIRMWARE_PROJECT "\","
             "\"version\":\"" FIRMWARE_VERSION "\","
             "\"device_type\":\"" FIRMWARE_DEVICE_TYPE "\","
             "\"target\":\"" CONFIG_IDF_TARGET "\""
             "},"
             "\"capabilities\":[\"capture\",\"stream\"],"
             "\"endpoints\":{"
             "\"root\":\"http://%s/\","
             "\"capture\":\"http://%s/capture\","
             "\"stream\":\"http://%s/stream\""
             "},"
             "\"status\":{\"ip\":\"%s\"}"
             "}",
             current_ip, current_ip, current_ip, current_ip);

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 2500,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) {
        return;
    }

    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_header(client, "X-Device-Id", device_id);
    esp_http_client_set_post_field(client, body, strlen(body));
    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);

    if (err == ESP_OK && status >= 200 && status < 300) {
        ESP_LOGI(TAG, "Registered with command server as %s", device_id);
    } else {
        ESP_LOGW(TAG, "Command server registration failed: err=%s status=%d", esp_err_to_name(err), status);
    }
}

static void start_mdns_service(void)
{
    char instance[80] = {0};
    snprintf(instance, sizeof(instance), "TimerCamera-X %s", device_id);

    mdns_txt_item_t txt[] = {
        {"device_id", device_id},
        {"type", "camera"},
        {"model", "M5Stack TimerCamera-X"},
        {"capture", "/capture"},
        {"stream", "/stream"},
    };

    esp_err_t err = mdns_init();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "mDNS init failed: %s", esp_err_to_name(err));
        return;
    }

    ESP_ERROR_CHECK_WITHOUT_ABORT(mdns_hostname_set(device_id));
    ESP_ERROR_CHECK_WITHOUT_ABORT(mdns_instance_name_set(instance));
    ESP_ERROR_CHECK_WITHOUT_ABORT(mdns_service_add(instance, "_http", "_tcp", 80, txt, sizeof(txt) / sizeof(txt[0])));
    ESP_LOGI(TAG, "mDNS advertised: http://%s.local/", device_id);
}

static void registration_task(void *arg)
{
    while (true) {
        register_with_command_server();
        vTaskDelay(pdMS_TO_TICKS(REGISTRATION_INTERVAL_MS));
    }
}

static bool form_get(const char *body, const char *key, char *out, size_t out_len)
{
    const size_t key_len = strlen(key);
    const char *p = body;
    while (p && *p) {
        if (strncmp(p, key, key_len) == 0 && p[key_len] == '=') {
            const char *value = p + key_len + 1;
            const char *end = strchr(value, '&');
            char encoded[128] = {0};
            size_t len = end ? (size_t)(end - value) : strlen(value);
            if (len >= sizeof(encoded)) {
                len = sizeof(encoded) - 1;
            }
            memcpy(encoded, value, len);
            url_decode(out, out_len, encoded);
            return true;
        }
        p = strchr(p, '&');
        if (p) {
            ++p;
        }
    }
    return false;
}

static esp_err_t root_get(httpd_req_t *req)
{
    bool provisioning = strcmp(current_ip, "0.0.0.0") == 0;
    char page[1400];
    if (provisioning) {
        snprintf(page, sizeof(page),
                 "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
                 "<title>TimerCamera-X Setup</title></head><body>"
                 "<h1>TimerCamera-X Wi-Fi Setup</h1>"
                 "<form method='post' action='/save'>"
                 "<p><label>SSID<br><input name='ssid' maxlength='32' required></label></p>"
                 "<p><label>Password<br><input name='password' type='password' maxlength='64'></label></p>"
                 "<p><button type='submit'>Save and restart</button></p>"
                 "</form>"
                 "<p>After restart, the camera will try this network for 10 seconds. "
                 "If it fails, setup mode returns.</p>"
                 "</body></html>");
    } else {
        snprintf(page, sizeof(page),
                 "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
                 "<title>TimerCamera-X</title></head><body>"
                 "<h1>TimerCamera-X</h1>"
                 "<p>IP: %s</p>"
                 "<p><a href='/capture'>Single JPEG capture</a></p>"
                 "<p><a href='/stream'>MJPEG stream</a></p>"
                 "<img src='/stream' style='max-width:100%%;height:auto'>"
                 "</body></html>",
                 current_ip);
    }

    httpd_resp_set_type(req, "text/html");
    return httpd_resp_send(req, page, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t save_post(httpd_req_t *req)
{
    char body[256] = {0};
    int received = httpd_req_recv(req, body, sizeof(body) - 1);
    if (received <= 0) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "missing form body");
        return ESP_FAIL;
    }

    char ssid[33] = {0};
    char password[65] = {0};
    if (!form_get(body, "ssid", ssid, sizeof(ssid))) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "missing ssid");
        return ESP_FAIL;
    }
    form_get(body, "password", password, sizeof(password));

    ESP_LOGI(TAG, "Saving Wi-Fi credentials for SSID '%s'", ssid);
    ESP_ERROR_CHECK(creds_save(ssid, password));

    httpd_resp_set_type(req, "text/html");
    httpd_resp_sendstr(req, "<html><body><h1>Saved</h1><p>Restarting...</p></body></html>");
    vTaskDelay(pdMS_TO_TICKS(1000));
    esp_restart();
    return ESP_OK;
}

static esp_err_t capture_get(httpd_req_t *req)
{
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
        httpd_resp_send_500(req);
        return ESP_FAIL;
    }

    httpd_resp_set_type(req, "image/jpeg");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    esp_err_t err = httpd_resp_send(req, (const char *)fb->buf, fb->len);
    esp_camera_fb_return(fb);
    return err;
}

static esp_err_t stream_get(httpd_req_t *req)
{
    static const char *boundary = "123456789000000000000987654321";
    char header[128];
    int64_t last_frame = esp_timer_get_time();

    httpd_resp_set_type(req, "multipart/x-mixed-replace;boundary=123456789000000000000987654321");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");

    while (true) {
        camera_fb_t *fb = esp_camera_fb_get();
        if (!fb) {
            ESP_LOGW(TAG, "Camera capture failed");
            return ESP_FAIL;
        }

        if (httpd_resp_send_chunk(req, "--", 2) != ESP_OK ||
            httpd_resp_send_chunk(req, boundary, HTTPD_RESP_USE_STRLEN) != ESP_OK ||
            httpd_resp_send_chunk(req, "\r\n", 2) != ESP_OK) {
            esp_camera_fb_return(fb);
            break;
        }

        int header_len = snprintf(header, sizeof(header),
                                  "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
                                  (unsigned)fb->len);
        if (httpd_resp_send_chunk(req, header, header_len) != ESP_OK ||
            httpd_resp_send_chunk(req, (const char *)fb->buf, fb->len) != ESP_OK ||
            httpd_resp_send_chunk(req, "\r\n", 2) != ESP_OK) {
            esp_camera_fb_return(fb);
            break;
        }

        int64_t now = esp_timer_get_time();
        ESP_LOGI(TAG, "Frame %u bytes, %.2f fps", (unsigned)fb->len, 1000000.0 / (double)(now - last_frame));
        last_frame = now;

        esp_camera_fb_return(fb);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }

    return ESP_OK;
}

static void start_http_server(bool provisioning)
{
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.stack_size = 8192;
    config.server_port = 80;

    ESP_ERROR_CHECK(httpd_start(&server, &config));

    httpd_uri_t root = {.uri = "/", .method = HTTP_GET, .handler = root_get};
    ESP_ERROR_CHECK(httpd_register_uri_handler(server, &root));

    if (provisioning) {
        httpd_uri_t save = {.uri = "/save", .method = HTTP_POST, .handler = save_post};
        ESP_ERROR_CHECK(httpd_register_uri_handler(server, &save));
    } else {
        httpd_uri_t capture = {.uri = "/capture", .method = HTTP_GET, .handler = capture_get};
        httpd_uri_t stream = {.uri = "/stream", .method = HTTP_GET, .handler = stream_get};
        ESP_ERROR_CHECK(httpd_register_uri_handler(server, &capture));
        ESP_ERROR_CHECK(httpd_register_uri_handler(server, &stream));
    }
}

static void wifi_event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupSetBits(wifi_events, WIFI_FAIL_BIT);
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        snprintf(current_ip, sizeof(current_ip), IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(wifi_events, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init_common(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    wifi_events = xEventGroupCreate();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
}

static bool connect_home_wifi(const wifi_creds_t *creds)
{
    esp_netif_create_default_wifi_sta();
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL, NULL));

    wifi_config_t wifi_config = {0};
    strlcpy((char *)wifi_config.sta.ssid, creds->ssid, sizeof(wifi_config.sta.ssid));
    strlcpy((char *)wifi_config.sta.password, creds->password, sizeof(wifi_config.sta.password));
    wifi_config.sta.threshold.authmode = strlen(creds->password) ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    wifi_config.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    EventBits_t bits = xEventGroupWaitBits(wifi_events, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
                                           pdTRUE, pdFALSE, pdMS_TO_TICKS(10000));
    return (bits & WIFI_CONNECTED_BIT) != 0;
}

static void start_setup_ap(void)
{
    esp_netif_create_default_wifi_ap();

    wifi_config_t ap_config = {
        .ap = {
            .ssid = SETUP_AP_SSID,
            .ssid_len = strlen(SETUP_AP_SSID),
            .password = SETUP_AP_PASS,
            .max_connection = 4,
            .authmode = WIFI_AUTH_WPA_WPA2_PSK,
        },
    };

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "Setup AP started: SSID=%s password=%s url=http://192.168.4.1/",
             SETUP_AP_SSID, SETUP_AP_PASS);
    start_http_server(true);
}

static esp_err_t camera_init(void)
{
    camera_config_t config = {
        .pin_pwdn = CAM_PIN_PWDN,
        .pin_reset = CAM_PIN_RESET,
        .pin_xclk = CAM_PIN_XCLK,
        .pin_sccb_sda = CAM_PIN_SIOD,
        .pin_sccb_scl = CAM_PIN_SIOC,
        .pin_d7 = CAM_PIN_D7,
        .pin_d6 = CAM_PIN_D6,
        .pin_d5 = CAM_PIN_D5,
        .pin_d4 = CAM_PIN_D4,
        .pin_d3 = CAM_PIN_D3,
        .pin_d2 = CAM_PIN_D2,
        .pin_d1 = CAM_PIN_D1,
        .pin_d0 = CAM_PIN_D0,
        .pin_vsync = CAM_PIN_VSYNC,
        .pin_href = CAM_PIN_HREF,
        .pin_pclk = CAM_PIN_PCLK,
        .xclk_freq_hz = 20000000,
        .ledc_timer = LEDC_TIMER_0,
        .ledc_channel = LEDC_CHANNEL_0,
        .pixel_format = PIXFORMAT_JPEG,
        .frame_size = FRAMESIZE_QVGA,
        .jpeg_quality = 14,
        .fb_count = 2,
        .fb_location = CAMERA_FB_IN_PSRAM,
        .grab_mode = CAMERA_GRAB_LATEST,
    };

    ESP_LOGI(TAG, "PSRAM size: %u bytes", (unsigned)esp_psram_get_size());
    ESP_RETURN_ON_ERROR(esp_camera_init(&config), TAG, "camera init");

    sensor_t *sensor = esp_camera_sensor_get();
    if (sensor) {
        sensor->set_framesize(sensor, FRAMESIZE_QVGA);
        sensor->set_quality(sensor, 14);
    }

    return ESP_OK;
}

void app_main(void)
{
    init_device_id();

    gpio_set_direction(LED_PIN, GPIO_MODE_OUTPUT);
    gpio_set_direction(BAT_HOLD_PIN, GPIO_MODE_OUTPUT);
    gpio_set_level(LED_PIN, 0);
    gpio_set_level(BAT_HOLD_PIN, 1);

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    wifi_init_common();

    wifi_creds_t creds;
    if (creds_load(&creds) != ESP_OK) {
        ESP_LOGI(TAG, "No saved Wi-Fi credentials; entering setup mode");
        start_setup_ap();
        return;
    }

    ESP_LOGI(TAG, "Connecting to saved Wi-Fi SSID '%s'", creds.ssid);
    if (!connect_home_wifi(&creds)) {
        ESP_LOGW(TAG, "Failed to connect within 10 seconds; clearing credentials");
        creds_clear();
        vTaskDelay(pdMS_TO_TICKS(1000));
        esp_restart();
    }

    ESP_LOGI(TAG, "Connected. Camera URL: http://%s/", current_ip);
    ESP_ERROR_CHECK(camera_init());
    start_http_server(false);
    start_mdns_service();
    register_with_command_server();
    xTaskCreate(registration_task, "registration", 4096, NULL, 5, NULL);
}
