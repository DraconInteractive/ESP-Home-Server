#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "driver/i2s_std.h"
#include "esp_camera.h"
#include "esp_check.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_psram.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "mdns.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

static const char *TAG = "esp_eye";

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT BIT1
#define WIFI_MAX_RETRIES 5

#define CAM_PIN_PWDN   -1
#define CAM_PIN_RESET  -1
#define CAM_PIN_XCLK    4
#define CAM_PIN_SIOD   18
#define CAM_PIN_SIOC   23
#define CAM_PIN_D7     36
#define CAM_PIN_D6     37
#define CAM_PIN_D5     38
#define CAM_PIN_D4     39
#define CAM_PIN_D3     35
#define CAM_PIN_D2     14
#define CAM_PIN_D1     13
#define CAM_PIN_D0     34
#define CAM_PIN_VSYNC   5
#define CAM_PIN_HREF   27
#define CAM_PIN_PCLK   25
#define CAM_PIN_LED    22

#define MIC_I2S_PORT I2S_NUM_1
#define MIC_I2S_BCLK 26
#define MIC_I2S_WS   32
#define MIC_I2S_DIN  33

#define AUDIO_SAMPLE_RATE 16000
#define AUDIO_CAPTURE_MS 1000
#define AUDIO_SAMPLE_BYTES 2
#define AUDIO_CHANNELS 1
#define AUDIO_WAV_HEADER_BYTES 44

static EventGroupHandle_t s_wifi_event_group;
static httpd_handle_t s_http_server;
static i2s_chan_handle_t s_i2s_rx_chan;
static int s_wifi_retry_count;
static bool s_wifi_ready;
static bool s_camera_ready;
static bool s_audio_ready;
static char s_device_id[48] = "esp-eye-unknown";
static char s_ip_addr[16] = "0.0.0.0";

static void put_le16(uint8_t *dst, uint16_t value)
{
    dst[0] = value & 0xff;
    dst[1] = (value >> 8) & 0xff;
}

static void put_le32(uint8_t *dst, uint32_t value)
{
    dst[0] = value & 0xff;
    dst[1] = (value >> 8) & 0xff;
    dst[2] = (value >> 16) & 0xff;
    dst[3] = (value >> 24) & 0xff;
}

static void write_wav_header(uint8_t *header, uint32_t data_bytes)
{
    const uint32_t byte_rate = AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * AUDIO_SAMPLE_BYTES;
    const uint16_t block_align = AUDIO_CHANNELS * AUDIO_SAMPLE_BYTES;

    memcpy(header + 0, "RIFF", 4);
    put_le32(header + 4, 36 + data_bytes);
    memcpy(header + 8, "WAVE", 4);
    memcpy(header + 12, "fmt ", 4);
    put_le32(header + 16, 16);
    put_le16(header + 20, 1);
    put_le16(header + 22, AUDIO_CHANNELS);
    put_le32(header + 24, AUDIO_SAMPLE_RATE);
    put_le32(header + 28, byte_rate);
    put_le16(header + 32, block_align);
    put_le16(header + 34, AUDIO_SAMPLE_BYTES * 8);
    memcpy(header + 36, "data", 4);
    put_le32(header + 40, data_bytes);
}

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
    char body[768] = {0};
    int status = 0;
    bool psram_ready = esp_psram_is_initialized();

    snprintf(path, sizeof(path), "/devices/%s/register", s_device_id);
    snprintf(body, sizeof(body),
             "{"
             "\"type\":\"camera\","
             "\"model\":\"ESP-EYE v2.1\","
             "\"capabilities\":[\"capture\",\"stream\",\"microphone\"],"
             "\"endpoints\":{"
             "\"root\":\"http://%s/\","
             "\"capture\":\"http://%s/capture\","
             "\"stream\":\"http://%s/stream\","
             "\"audio\":\"http://%s/audio\""
             "},"
             "\"status\":{"
             "\"ip\":\"%s\","
             "\"camera\":\"OV2640\","
             "\"microphone\":\"MEMS\","
             "\"psram_ready\":%s,"
             "\"camera_ready\":%s,"
             "\"audio_ready\":%s,"
             "\"flash_mb\":4,"
             "\"stage\":\"camera-audio-http\""
             "}"
             "}",
             s_ip_addr, s_ip_addr, s_ip_addr, s_ip_addr,
             s_ip_addr,
             psram_ready ? "true" : "false",
             s_camera_ready ? "true" : "false",
             s_audio_ready ? "true" : "false");

    esp_err_t err = post_json(path, body, &status);
    if (err == ESP_OK && status >= 200 && status < 300) {
        ESP_LOGI(TAG, "Registered with command server");
    } else {
        ESP_LOGW(TAG, "Registration failed: err=%s status=%d", esp_err_to_name(err), status);
    }
}

static esp_err_t root_get(httpd_req_t *req)
{
    char page[900] = {0};
    snprintf(page, sizeof(page),
             "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
             "<title>ESP-EYE</title></head><body>"
             "<h1>ESP-EYE</h1>"
             "<p>Device: %s</p>"
             "<p>IP: %s</p>"
             "<p>Camera: %s</p>"
             "<p><a href='/capture'>Single JPEG capture</a></p>"
             "<p><a href='/stream'>MJPEG stream</a></p>"
             "<p><a href='/audio'>One second WAV audio capture</a></p>"
             "<img src='/stream' style='max-width:100%%;height:auto'>"
             "</body></html>",
             s_device_id,
             s_ip_addr,
             s_camera_ready ? "ready" : "not ready");

    httpd_resp_set_type(req, "text/html");
    return httpd_resp_send(req, page, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t capture_get(httpd_req_t *req)
{
    if (!s_camera_ready) {
        httpd_resp_set_status(req, "503 Service Unavailable");
        httpd_resp_sendstr(req, "camera not ready");
        return ESP_FAIL;
    }

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
    if (!s_camera_ready) {
        httpd_resp_set_status(req, "503 Service Unavailable");
        httpd_resp_sendstr(req, "camera not ready");
        return ESP_FAIL;
    }

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

static esp_err_t audio_get(httpd_req_t *req)
{
    if (!s_audio_ready) {
        httpd_resp_set_status(req, "503 Service Unavailable");
        httpd_resp_sendstr(req, "audio not ready");
        return ESP_FAIL;
    }

    const size_t sample_count = (AUDIO_SAMPLE_RATE * AUDIO_CAPTURE_MS) / 1000;
    const size_t data_bytes = sample_count * AUDIO_SAMPLE_BYTES;
    uint8_t header[AUDIO_WAV_HEADER_BYTES] = {0};
    int16_t *pcm = malloc(data_bytes);
    int32_t raw[256];

    if (!pcm) {
        httpd_resp_send_500(req);
        return ESP_FAIL;
    }

    size_t written_samples = 0;
    while (written_samples < sample_count) {
        size_t bytes_read = 0;
        esp_err_t err = i2s_channel_read(s_i2s_rx_chan, raw, sizeof(raw), &bytes_read, pdMS_TO_TICKS(1000));
        if (err != ESP_OK) {
            free(pcm);
            ESP_LOGW(TAG, "I2S read failed: %s", esp_err_to_name(err));
            httpd_resp_send_500(req);
            return ESP_FAIL;
        }

        size_t raw_samples = bytes_read / sizeof(raw[0]);
        for (size_t i = 0; i < raw_samples && written_samples < sample_count; i++) {
            pcm[written_samples++] = (int16_t)(raw[i] >> 14);
        }
    }

    write_wav_header(header, data_bytes);
    httpd_resp_set_type(req, "audio/wav");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    esp_err_t err = httpd_resp_send_chunk(req, (const char *)header, sizeof(header));
    if (err == ESP_OK) {
        err = httpd_resp_send_chunk(req, (const char *)pcm, data_bytes);
    }
    if (err == ESP_OK) {
        err = httpd_resp_send_chunk(req, NULL, 0);
    }

    ESP_LOGI(TAG, "Audio capture %u bytes", (unsigned)data_bytes);
    free(pcm);
    return err;
}

static void start_http_server(void)
{
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.stack_size = 8192;
    config.server_port = 80;

    ESP_ERROR_CHECK(httpd_start(&s_http_server, &config));

    httpd_uri_t root = {.uri = "/", .method = HTTP_GET, .handler = root_get};
    httpd_uri_t capture = {.uri = "/capture", .method = HTTP_GET, .handler = capture_get};
    httpd_uri_t stream = {.uri = "/stream", .method = HTTP_GET, .handler = stream_get};
    httpd_uri_t audio = {.uri = "/audio", .method = HTTP_GET, .handler = audio_get};
    ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &root));
    ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &capture));
    ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &stream));
    ESP_ERROR_CHECK(httpd_register_uri_handler(s_http_server, &audio));
}

static void start_mdns_service(void)
{
    char instance[80] = {0};
    snprintf(instance, sizeof(instance), "ESP-EYE %s", s_device_id);

    mdns_txt_item_t txt[] = {
        {"device_id", s_device_id},
        {"type", "camera"},
        {"model", "ESP-EYE v2.1"},
        {"capture", "/capture"},
        {"stream", "/stream"},
        {"audio", "/audio"},
    };

    esp_err_t err = mdns_init();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "mDNS init failed: %s", esp_err_to_name(err));
        return;
    }

    ESP_ERROR_CHECK_WITHOUT_ABORT(mdns_hostname_set(s_device_id));
    ESP_ERROR_CHECK_WITHOUT_ABORT(mdns_instance_name_set(instance));
    ESP_ERROR_CHECK_WITHOUT_ABORT(mdns_service_add(instance, "_http", "_tcp", 80, txt, sizeof(txt) / sizeof(txt[0])));
    ESP_LOGI(TAG, "mDNS advertised: http://%s.local/", s_device_id);
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

    s_camera_ready = true;
    return ESP_OK;
}

static esp_err_t audio_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(MIC_I2S_PORT, I2S_ROLE_MASTER);
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, NULL, &s_i2s_rx_chan), TAG, "i2s new channel");

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(AUDIO_SAMPLE_RATE),
        .slot_cfg = I2S_STD_MSB_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = MIC_I2S_BCLK,
            .ws = MIC_I2S_WS,
            .dout = I2S_GPIO_UNUSED,
            .din = MIC_I2S_DIN,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };

    std_cfg.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(s_i2s_rx_chan, &std_cfg), TAG, "i2s std init");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(s_i2s_rx_chan), TAG, "i2s enable");
    s_audio_ready = true;
    ESP_LOGI(TAG, "Audio ready: %d Hz mono WAV on /audio", AUDIO_SAMPLE_RATE);
    return ESP_OK;
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

    if (s_wifi_ready) {
        esp_err_t camera_err = camera_init();
        if (camera_err != ESP_OK) {
            ESP_LOGE(TAG, "Camera init failed: %s", esp_err_to_name(camera_err));
        }
        esp_err_t audio_err = audio_init();
        if (audio_err != ESP_OK) {
            ESP_LOGE(TAG, "Audio init failed: %s", esp_err_to_name(audio_err));
        }
        start_http_server();
        start_mdns_service();
        ESP_LOGI(TAG, "ESP-EYE URL: http://%s/", s_ip_addr);
    }

    register_with_server();
    xTaskCreate(registration_task, "registration", 4096, NULL, 4, NULL);
}
