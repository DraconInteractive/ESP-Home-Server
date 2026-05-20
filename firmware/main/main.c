#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2c.h"
#include "driver/i2s_std.h"
#include "driver/spi_master.h"
#include "esp_chip_info.h"
#include "esp_check.h"
#include "esp_event.h"
#include "esp_err.h"
#include "esp_flash.h"
#include "esp_http_client.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "esp_lcd_sh8601.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_touch.h"
#include "esp_lcd_touch_ft5x06.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_sleep.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "es8311.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "hal/spi_types.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

static const char *TAG = "waveshare_c6";

#define LCD_HOST SPI2_HOST
#define I2C_PORT I2C_NUM_0

#define LCD_H_RES 368
#define LCD_V_RES 448
#define LCD_BIT_PER_PIXEL 16
#define UI_STATUS_H 18
#define UI_MARGIN 18
#define UI_TEXT_X 18
#define UI_TEXT_Y 48
#define UI_TEXT_W (LCD_H_RES - (UI_TEXT_X * 2))
#define UI_TEXT_H 300
#define UI_METER_X 32
#define UI_METER_Y 390
#define UI_METER_W (LCD_H_RES - (UI_METER_X * 2))
#define UI_METER_H 12
#define UI_FONT_SCALE 2
#define UI_FONT_W 5
#define UI_FONT_H 7
#define UI_CHAR_W ((UI_FONT_W + 1) * UI_FONT_SCALE)
#define UI_LINE_H ((UI_FONT_H + 2) * UI_FONT_SCALE)
#define DEVICE_EVENT_POLL_INTERVAL_US (5LL * 1000LL * 1000LL)
#define ALERT_DISPLAY_TIME_US (5LL * 1000LL * 1000LL)

#define LCD_PIN_CS GPIO_NUM_5
#define LCD_PIN_PCLK GPIO_NUM_0
#define LCD_PIN_DATA0 GPIO_NUM_1
#define LCD_PIN_DATA1 GPIO_NUM_2
#define LCD_PIN_DATA2 GPIO_NUM_3
#define LCD_PIN_DATA3 GPIO_NUM_4
#define LCD_PIN_RST (-1)

#define TOUCH_PIN_INT GPIO_NUM_15
#define TOUCH_PIN_RST (-1)

#define I2C_PIN_SCL GPIO_NUM_7
#define I2C_PIN_SDA GPIO_NUM_8
#define I2C_FREQ_HZ 400000

#define TCA9554_ADDR 0x20
#define TCA9554_REG_OUTPUT 0x01
#define TCA9554_REG_CONFIG 0x03
#define TCA9554_LCD_RST_BIT BIT(4)
#define TCA9554_TOUCH_RST_BIT BIT(5)
#define TCA9554_MIC_EN_BIT BIT(7)

#define AXP2101_ADDR 0x34
#define AXP2101_REG_STATUS1 0x00
#define AXP2101_REG_STATUS2 0x01
#define AXP2101_REG_COMMON_CONFIG 0x10
#define AXP2101_REG_ADC_CHANNEL_CTRL 0x30
#define AXP2101_REG_ADC_BAT_H 0x34
#define AXP2101_REG_ADC_BAT_L 0x35
#define AXP2101_REG_ADC_VBUS_H 0x38
#define AXP2101_REG_ADC_VBUS_L 0x39
#define AXP2101_REG_ADC_VSYS_H 0x3A
#define AXP2101_REG_ADC_VSYS_L 0x3B
#define AXP2101_REG_INTEN2 0x41
#define AXP2101_REG_INTSTS1 0x48
#define AXP2101_REG_INTSTS2 0x49
#define AXP2101_REG_BAT_PERCENT 0xA4
#define AXP2101_ADC_ENABLE_BATT BIT(0)
#define AXP2101_ADC_ENABLE_VBUS BIT(2)
#define AXP2101_ADC_ENABLE_VSYS BIT(3)
#define AXP2101_STATUS1_VBUS_GOOD BIT(5)
#define AXP2101_STATUS1_BAT_PRESENT BIT(3)
#define AXP2101_STATUS2_VBUS_ABSENT BIT(3)
#define AXP2101_CHARGE_STATE_MASK 0x07
#define AXP2101_POWER_STATE_SHIFT 5
#define AXP2101_POWER_STATE_CHARGING 0x01
#define AXP2101_POWER_STATE_DISCHARGING 0x02
#define AXP2101_SHUTDOWN_BIT BIT(0)
#define AXP2101_PKEY_SHORT_IRQ BIT(3)
#define AXP2101_PKEY_IRQ_MASK AXP2101_PKEY_SHORT_IRQ

#define AUDIO_SAMPLE_RATE 16000
#define AUDIO_RECV_SAMPLES 512
#define AUDIO_VISUAL_GAIN 8
#define COMMAND_CAPTURE_SAMPLES ((AUDIO_SAMPLE_RATE * CONFIG_SPOKEN_COMMAND_CAPTURE_MS) / 1000)
#define COMMAND_HTTP_RESPONSE_MAX 512
#define COMMAND_TEXT_MAX 192
#define HTTP_CHUNK_HEADER_MAX 16
#define DEVICE_EVENTS_URL_MAX 192
#define DEVICE_REGISTER_URL_MAX 192
#define TONE_CHUNK_FRAMES 128
#define WIFI_CONNECT_TIMEOUT_MS 12000
#define WIFI_CONNECTED_BIT BIT(0)
#define WIFI_FAIL_BIT BIT(1)
#define WIFI_MAX_RETRY 5
#define ES8311_VOICE_VOLUME 85
#define ES8311_MIC_GAIN ES8311_MIC_GAIN_18DB

#define I2S_PORT I2S_NUM_0
#define I2S_PIN_MCLK GPIO_NUM_19
#define I2S_PIN_BCLK GPIO_NUM_20
#define I2S_PIN_DIN GPIO_NUM_21
#define I2S_PIN_WS GPIO_NUM_22
#define I2S_PIN_DOUT GPIO_NUM_23

#define BUTTON_PIN_BOOT GPIO_NUM_9
#define BUTTON_DEBOUNCE_MS 35

#define QMI8658_ADDR_HIGH 0x6A
#define QMI8658_ADDR_LOW 0x6B
#define QMI8658_WHOAMI_VALUE 0x05
#define QMI8658_REG_WHOAMI 0x00
#define QMI8658_REG_CTRL1 0x02
#define QMI8658_REG_CTRL2 0x03
#define QMI8658_REG_CTRL3 0x04
#define QMI8658_REG_CTRL7 0x08
#define QMI8658_REG_STATUS0 0x2E
#define QMI8658_REG_TEMP_L 0x33
#define QMI8658_REG_RESET 0x60
#define QMI8658_REG_RST_RESULT 0x4D
#define QMI8658_RESET_VALUE 0xB0
#define QMI8658_RST_RESULT_VALUE 0x80
#define QMI8658_ACCEL_SCALE_G (8.0f / 32768.0f)
#define QMI8658_GYRO_SCALE_DPS (512.0f / 32768.0f)

static const sh8601_lcd_init_cmd_t lcd_init_cmds[] = {
    {0x11, (uint8_t[]){0x00}, 0, 120},
    {0x44, (uint8_t[]){0x01, 0xD1}, 2, 0},
    {0x35, (uint8_t[]){0x00}, 1, 0},
    {0x53, (uint8_t[]){0x20}, 1, 10},
    {0x2A, (uint8_t[]){0x00, 0x00, 0x01, 0x6F}, 4, 0},
    {0x2B, (uint8_t[]){0x00, 0x00, 0x01, 0xBF}, 4, 0},
    {0x51, (uint8_t[]){0x00}, 1, 10},
    {0x29, (uint8_t[]){0x00}, 0, 10},
    {0x51, (uint8_t[]){0xFF}, 1, 0},
};

typedef struct {
    esp_lcd_panel_handle_t panel;
    esp_lcd_touch_handle_t touch;
} app_display_t;

typedef struct {
    float ax_g;
    float ay_g;
    float az_g;
    float gx_dps;
    float gy_dps;
    float gz_dps;
    float temperature_c;
} imu_sample_t;

typedef struct {
    bool valid;
    bool battery_present;
    bool vbus_present;
    bool vbus_good;
    bool charging;
    bool discharging;
    uint8_t charge_state;
    int percent;
    uint16_t battery_mv;
    uint16_t vbus_mv;
    uint16_t system_mv;
} power_status_t;

static uint8_t s_qmi8658_addr = QMI8658_ADDR_LOW;
static uint8_t s_tca9554_output = 0x00;
static bool s_visualizer_enabled = true;
static es8311_handle_t s_es8311 = NULL;
static i2s_chan_handle_t s_i2s_tx = NULL;
static i2s_chan_handle_t s_i2s_rx = NULL;
static int16_t s_audio_samples[AUDIO_RECV_SAMPLES * 2] = {0};
static int16_t s_command_chunk[AUDIO_RECV_SAMPLES] = {0};
static int16_t s_tone_chunk[TONE_CHUNK_FRAMES * 2] = {0};
static char s_last_command_text[COMMAND_TEXT_MAX] = "Hold BOOT and speak.";
static char s_device_id[40] = "waveshare-c6-unknown";
static EventGroupHandle_t s_wifi_event_group = NULL;
static int s_wifi_retry_count = 0;
static bool s_wifi_ready = false;

static esp_err_t set_visualizer_enabled(esp_lcd_panel_handle_t panel, bool enabled);
static void render_power_status(esp_lcd_panel_handle_t panel);
static void enter_power_off(esp_lcd_panel_handle_t panel);
static esp_err_t poll_device_events(esp_lcd_panel_handle_t panel, esp_lcd_touch_handle_t touch);

static esp_err_t tca9554_write(uint8_t reg, uint8_t value)
{
    uint8_t payload[] = {reg, value};
    return i2c_master_write_to_device(I2C_PORT, TCA9554_ADDR, payload, sizeof(payload), pdMS_TO_TICKS(100));
}

static esp_err_t tca9554_set_output(uint8_t value)
{
    ESP_RETURN_ON_ERROR(tca9554_write(TCA9554_REG_OUTPUT, value), TAG, "write TCA9554 output");
    s_tca9554_output = value;
    return ESP_OK;
}

static esp_err_t set_mic_power(bool enabled)
{
    uint8_t next_output = s_tca9554_output;
    if (enabled) {
        next_output |= TCA9554_MIC_EN_BIT;
    } else {
        next_output &= (uint8_t)~TCA9554_MIC_EN_BIT;
    }
    return tca9554_set_output(next_output);
}

static esp_err_t i2c_reg_write(uint8_t dev_addr, uint8_t reg, uint8_t value)
{
    uint8_t payload[] = {reg, value};
    return i2c_master_write_to_device(I2C_PORT, dev_addr, payload, sizeof(payload), pdMS_TO_TICKS(100));
}

static esp_err_t i2c_reg_read(uint8_t dev_addr, uint8_t reg, uint8_t *data, size_t len)
{
    return i2c_master_write_read_device(I2C_PORT, dev_addr, &reg, 1, data, len, pdMS_TO_TICKS(100));
}

static esp_err_t i2c_reg_read_h5l8(uint8_t dev_addr, uint8_t high_reg, uint8_t low_reg, uint16_t *value)
{
    uint8_t raw[2] = {0};
    (void)low_reg;
    ESP_RETURN_ON_ERROR(i2c_reg_read(dev_addr, high_reg, raw, 2), TAG, "read h5l8 register 0x%02x", high_reg);
    *value = ((uint16_t)(raw[0] & 0x1F) << 8) | raw[1];
    return ESP_OK;
}

static esp_err_t i2c_reg_read_h6l8(uint8_t dev_addr, uint8_t high_reg, uint8_t low_reg, uint16_t *value)
{
    uint8_t raw[2] = {0};
    (void)low_reg;
    ESP_RETURN_ON_ERROR(i2c_reg_read(dev_addr, high_reg, raw, 2), TAG, "read h6l8 register 0x%02x", high_reg);
    *value = ((uint16_t)(raw[0] & 0x3F) << 8) | raw[1];
    return ESP_OK;
}

static esp_err_t i2c_reg_update(uint8_t dev_addr, uint8_t reg, uint8_t mask, uint8_t value)
{
    uint8_t current = 0;
    ESP_RETURN_ON_ERROR(i2c_reg_read(dev_addr, reg, &current, 1), TAG, "read register 0x%02x", reg);
    current = (current & mask) | value;
    return i2c_reg_write(dev_addr, reg, current);
}

static esp_err_t i2c_init(void)
{
    const i2c_config_t conf = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = I2C_PIN_SDA,
        .scl_io_num = I2C_PIN_SCL,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = I2C_FREQ_HZ,
    };

    ESP_RETURN_ON_ERROR(i2c_param_config(I2C_PORT, &conf), TAG, "configure I2C");
    return i2c_driver_install(I2C_PORT, conf.mode, 0, 0, 0);
}

static esp_err_t reset_lcd_and_touch(void)
{
    const uint8_t output_mask = TCA9554_LCD_RST_BIT | TCA9554_TOUCH_RST_BIT | TCA9554_MIC_EN_BIT;
    const uint8_t reset_mask = TCA9554_LCD_RST_BIT | TCA9554_TOUCH_RST_BIT;

    ESP_LOGI(TAG, "Reset LCD/touch and enable mic rail through TCA9554");
    ESP_RETURN_ON_ERROR(tca9554_write(TCA9554_REG_CONFIG, (uint8_t)~output_mask), TAG, "configure TCA9554 pins");
    ESP_RETURN_ON_ERROR(tca9554_set_output(0x00), TAG, "assert reset");
    vTaskDelay(pdMS_TO_TICKS(200));
    ESP_RETURN_ON_ERROR(tca9554_set_output(reset_mask | TCA9554_MIC_EN_BIT), TAG, "release reset and enable mic");
    vTaskDelay(pdMS_TO_TICKS(120));

    return ESP_OK;
}

static esp_err_t buttons_init(void)
{
    const gpio_config_t boot_button = {
        .pin_bit_mask = 1ULL << BUTTON_PIN_BOOT,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };

    ESP_LOGI(TAG, "Initialize BOOT button on GPIO%d for command capture", BUTTON_PIN_BOOT);
    return gpio_config(&boot_button);
}

static bool boot_button_pressed(void)
{
    static int last_raw = 1;
    static int stable_level = 1;
    static uint32_t changed_at_ms = 0;

    const int raw = gpio_get_level(BUTTON_PIN_BOOT);
    const uint32_t now_ms = esp_log_timestamp();

    if (raw != last_raw) {
        last_raw = raw;
        changed_at_ms = now_ms;
    }

    if ((now_ms - changed_at_ms) < BUTTON_DEBOUNCE_MS || raw == stable_level) {
        return false;
    }

    stable_level = raw;
    return stable_level == 0;
}

static bool boot_button_is_held(void)
{
    return gpio_get_level(BUTTON_PIN_BOOT) == 0;
}

static esp_err_t axp2101_init_power_button_irq(void)
{
    uint8_t enabled = 0;
    uint8_t adc_ctrl = 0;

    ESP_LOGI(TAG, "Initialize AXP2101 PMU status and PWR short-press IRQ polling");
    ESP_RETURN_ON_ERROR(i2c_reg_read(AXP2101_ADDR, AXP2101_REG_ADC_CHANNEL_CTRL, &adc_ctrl, 1), TAG, "read AXP2101 ADC channel control");
    adc_ctrl |= AXP2101_ADC_ENABLE_BATT | AXP2101_ADC_ENABLE_VBUS | AXP2101_ADC_ENABLE_VSYS;
    ESP_RETURN_ON_ERROR(i2c_reg_write(AXP2101_ADDR, AXP2101_REG_ADC_CHANNEL_CTRL, adc_ctrl), TAG, "enable AXP2101 voltage ADCs");
    ESP_RETURN_ON_ERROR(i2c_reg_write(AXP2101_ADDR, AXP2101_REG_INTSTS1, 0xFF), TAG, "clear AXP2101 INTSTS1");
    ESP_RETURN_ON_ERROR(i2c_reg_write(AXP2101_ADDR, AXP2101_REG_INTSTS1 + 1, 0xFF), TAG, "clear AXP2101 INTSTS2");
    ESP_RETURN_ON_ERROR(i2c_reg_write(AXP2101_ADDR, AXP2101_REG_INTSTS1 + 2, 0xFF), TAG, "clear AXP2101 INTSTS3");
    ESP_RETURN_ON_ERROR(i2c_reg_read(AXP2101_ADDR, AXP2101_REG_INTEN2, &enabled, 1), TAG, "read AXP2101 INTEN2");
    enabled |= AXP2101_PKEY_IRQ_MASK;
    return i2c_reg_write(AXP2101_ADDR, AXP2101_REG_INTEN2, enabled);
}

static esp_err_t axp2101_clear_power_irqs(void)
{
    ESP_RETURN_ON_ERROR(i2c_reg_write(AXP2101_ADDR, AXP2101_REG_INTSTS1, 0xFF), TAG, "clear AXP2101 INTSTS1");
    ESP_RETURN_ON_ERROR(i2c_reg_write(AXP2101_ADDR, AXP2101_REG_INTSTS1 + 1, 0xFF), TAG, "clear AXP2101 INTSTS2");
    ESP_RETURN_ON_ERROR(i2c_reg_write(AXP2101_ADDR, AXP2101_REG_INTSTS1 + 2, 0xFF), TAG, "clear AXP2101 INTSTS3");
    return ESP_OK;
}

static bool axp2101_power_button_short_pressed(void)
{
    uint8_t status = 0;
    esp_err_t ret = i2c_reg_read(AXP2101_ADDR, AXP2101_REG_INTSTS2, &status, 1);
    if (ret != ESP_OK) {
        return false;
    }

    const uint8_t pkey_status = status & AXP2101_PKEY_IRQ_MASK;
    if (pkey_status == 0) {
        return false;
    }

    ESP_LOGI(TAG, "AXP2101 PWR IRQ status=0x%02x", status);
    ESP_ERROR_CHECK(i2c_reg_write(AXP2101_ADDR, AXP2101_REG_INTSTS2, pkey_status));
    return (pkey_status & AXP2101_PKEY_SHORT_IRQ) != 0;
}

static esp_err_t axp2101_read_power_status(power_status_t *status)
{
    uint8_t status1 = 0;
    uint8_t status2 = 0;
    uint8_t percent = 0;

    memset(status, 0, sizeof(*status));
    ESP_RETURN_ON_ERROR(i2c_reg_read(AXP2101_ADDR, AXP2101_REG_STATUS1, &status1, 1), TAG, "read AXP2101 STATUS1");
    ESP_RETURN_ON_ERROR(i2c_reg_read(AXP2101_ADDR, AXP2101_REG_STATUS2, &status2, 1), TAG, "read AXP2101 STATUS2");

    status->valid = true;
    status->battery_present = (status1 & AXP2101_STATUS1_BAT_PRESENT) != 0;
    status->vbus_good = (status1 & AXP2101_STATUS1_VBUS_GOOD) != 0;
    status->vbus_present = status->vbus_good && ((status2 & AXP2101_STATUS2_VBUS_ABSENT) == 0);
    status->charging = (status2 >> AXP2101_POWER_STATE_SHIFT) == AXP2101_POWER_STATE_CHARGING;
    status->discharging = (status2 >> AXP2101_POWER_STATE_SHIFT) == AXP2101_POWER_STATE_DISCHARGING;
    status->charge_state = status2 & AXP2101_CHARGE_STATE_MASK;
    status->percent = -1;

    if (status->battery_present) {
        ESP_RETURN_ON_ERROR(i2c_reg_read(AXP2101_ADDR, AXP2101_REG_BAT_PERCENT, &percent, 1), TAG, "read AXP2101 battery percent");
        status->percent = percent;
        ESP_RETURN_ON_ERROR(i2c_reg_read_h5l8(AXP2101_ADDR, AXP2101_REG_ADC_BAT_H, AXP2101_REG_ADC_BAT_L, &status->battery_mv), TAG, "read AXP2101 battery voltage");
    }
    if (status->vbus_present) {
        ESP_RETURN_ON_ERROR(i2c_reg_read_h6l8(AXP2101_ADDR, AXP2101_REG_ADC_VBUS_H, AXP2101_REG_ADC_VBUS_L, &status->vbus_mv), TAG, "read AXP2101 VBUS voltage");
    }
    ESP_RETURN_ON_ERROR(i2c_reg_read_h6l8(AXP2101_ADDR, AXP2101_REG_ADC_VSYS_H, AXP2101_REG_ADC_VSYS_L, &status->system_mv), TAG, "read AXP2101 system voltage");
    return ESP_OK;
}

static esp_err_t axp2101_shutdown(void)
{
    return i2c_reg_update(AXP2101_ADDR, AXP2101_REG_COMMON_CONFIG, AXP2101_SHUTDOWN_BIT, AXP2101_SHUTDOWN_BIT);
}

static esp_err_t storage_init(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_RETURN_ON_ERROR(nvs_flash_erase(), TAG, "erase NVS");
        ret = nvs_flash_init();
    }
    return ret;
}

static void wifi_event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_wifi_retry_count < WIFI_MAX_RETRY) {
            s_wifi_retry_count++;
            esp_wifi_connect();
            ESP_LOGI(TAG, "Wi-Fi retry %d/%d", s_wifi_retry_count, WIFI_MAX_RETRY);
        } else {
            xEventGroupSetBits(s_wifi_event_group, WIFI_FAIL_BIT);
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *event = (const ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Wi-Fi connected ip=" IPSTR, IP2STR(&event->ip_info.ip));
        s_wifi_retry_count = 0;
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static esp_err_t wifi_init_sta(void)
{
    if (strlen(CONFIG_SPOKEN_COMMAND_WIFI_SSID) == 0) {
        ESP_LOGW(TAG, "Wi-Fi disabled: CONFIG_SPOKEN_COMMAND_WIFI_SSID is empty");
        return ESP_ERR_INVALID_STATE;
    }

    s_wifi_event_group = xEventGroupCreate();
    ESP_RETURN_ON_FALSE(s_wifi_event_group != NULL, ESP_ERR_NO_MEM, TAG, "create Wi-Fi event group");

    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "init esp_netif");
    ESP_RETURN_ON_ERROR(esp_event_loop_create_default(), TAG, "create default event loop");
    esp_netif_create_default_wifi_sta();

    const wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_RETURN_ON_ERROR(esp_wifi_init(&init_config), TAG, "init Wi-Fi");
    ESP_RETURN_ON_ERROR(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, NULL), TAG, "register Wi-Fi handler");
    ESP_RETURN_ON_ERROR(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, NULL), TAG, "register IP handler");

    wifi_config_t wifi_config = {0};
    strlcpy((char *)wifi_config.sta.ssid, CONFIG_SPOKEN_COMMAND_WIFI_SSID, sizeof(wifi_config.sta.ssid));
    strlcpy((char *)wifi_config.sta.password, CONFIG_SPOKEN_COMMAND_WIFI_PASSWORD, sizeof(wifi_config.sta.password));
    wifi_config.sta.threshold.authmode = strlen(CONFIG_SPOKEN_COMMAND_WIFI_PASSWORD) > 0 ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;

    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG, "set Wi-Fi mode");
    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_STA, &wifi_config), TAG, "set Wi-Fi config");
    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "start Wi-Fi");

    EventBits_t bits = xEventGroupWaitBits(
        s_wifi_event_group,
        WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
        pdFALSE,
        pdFALSE,
        pdMS_TO_TICKS(WIFI_CONNECT_TIMEOUT_MS));

    s_wifi_ready = (bits & WIFI_CONNECTED_BIT) != 0;
    if (!s_wifi_ready) {
        ESP_LOGW(TAG, "Wi-Fi not connected; command uploads disabled until restart");
        return ESP_ERR_TIMEOUT;
    }

    ESP_LOGI(TAG, "Command server URL: %s", CONFIG_SPOKEN_COMMAND_SERVER_URL);
    return ESP_OK;
}

static void init_device_id(void)
{
    uint8_t mac[6] = {0};
    if (esp_read_mac(mac, ESP_MAC_WIFI_STA) == ESP_OK) {
        snprintf(s_device_id, sizeof(s_device_id), "waveshare-c6-%02x%02x%02x", mac[3], mac[4], mac[5]);
    }
    ESP_LOGI(TAG, "Device ID: %s", s_device_id);
}

static uint16_t rgb565(uint8_t r, uint8_t g, uint8_t b)
{
    uint16_t color = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3);
    return SPI_SWAP_DATA_TX(color, 16);
}

static int clamp_int(int value, int min_value, int max_value)
{
    if (value < min_value) {
        return min_value;
    }
    if (value > max_value) {
        return max_value;
    }
    return value;
}

static float clamp_float(float value, float min_value, float max_value)
{
    if (value < min_value) {
        return min_value;
    }
    if (value > max_value) {
        return max_value;
    }
    return value;
}

static void fill_rect(esp_lcd_panel_handle_t panel, uint16_t *buffer, int x0, int y0, int x1, int y1, uint16_t color)
{
    x0 = clamp_int(x0, 0, LCD_H_RES);
    x1 = clamp_int(x1, 0, LCD_H_RES);
    y0 = clamp_int(y0, 0, LCD_V_RES);
    y1 = clamp_int(y1, 0, LCD_V_RES);
    if (x1 <= x0 || y1 <= y0) {
        return;
    }

    const int width = x1 - x0;
    const int height = y1 - y0;
    const int pixels = width * height;

    for (int i = 0; i < pixels; ++i) {
        buffer[i] = color;
    }

    ESP_ERROR_CHECK(esp_lcd_panel_draw_bitmap(panel, x0, y0, x1, y1, buffer));
}

static void fill_rect_chunked(esp_lcd_panel_handle_t panel, uint16_t *buffer, int max_rows,
                              int x0, int y0, int x1, int y1, uint16_t color)
{
    for (int y = y0; y < y1; y += max_rows) {
        const int next_y = (y + max_rows < y1) ? y + max_rows : y1;
        fill_rect(panel, buffer, x0, y, x1, next_y, color);
    }
}

static const uint8_t *font5x7(char c)
{
    static const uint8_t blank[5] = {0x00, 0x00, 0x00, 0x00, 0x00};
    static const uint8_t glyph_0[5] = {0x3e, 0x51, 0x49, 0x45, 0x3e};
    static const uint8_t glyph_1[5] = {0x00, 0x42, 0x7f, 0x40, 0x00};
    static const uint8_t glyph_2[5] = {0x42, 0x61, 0x51, 0x49, 0x46};
    static const uint8_t glyph_3[5] = {0x21, 0x41, 0x45, 0x4b, 0x31};
    static const uint8_t glyph_4[5] = {0x18, 0x14, 0x12, 0x7f, 0x10};
    static const uint8_t glyph_5[5] = {0x27, 0x45, 0x45, 0x45, 0x39};
    static const uint8_t glyph_6[5] = {0x3c, 0x4a, 0x49, 0x49, 0x30};
    static const uint8_t glyph_7[5] = {0x01, 0x71, 0x09, 0x05, 0x03};
    static const uint8_t glyph_8[5] = {0x36, 0x49, 0x49, 0x49, 0x36};
    static const uint8_t glyph_9[5] = {0x06, 0x49, 0x49, 0x29, 0x1e};
    static const uint8_t glyph_a[5] = {0x7e, 0x11, 0x11, 0x11, 0x7e};
    static const uint8_t glyph_b[5] = {0x7f, 0x49, 0x49, 0x49, 0x36};
    static const uint8_t glyph_c[5] = {0x3e, 0x41, 0x41, 0x41, 0x22};
    static const uint8_t glyph_d[5] = {0x7f, 0x41, 0x41, 0x22, 0x1c};
    static const uint8_t glyph_e[5] = {0x7f, 0x49, 0x49, 0x49, 0x41};
    static const uint8_t glyph_f[5] = {0x7f, 0x09, 0x09, 0x09, 0x01};
    static const uint8_t glyph_g[5] = {0x3e, 0x41, 0x49, 0x49, 0x7a};
    static const uint8_t glyph_h[5] = {0x7f, 0x08, 0x08, 0x08, 0x7f};
    static const uint8_t glyph_i[5] = {0x00, 0x41, 0x7f, 0x41, 0x00};
    static const uint8_t glyph_j[5] = {0x20, 0x40, 0x41, 0x3f, 0x01};
    static const uint8_t glyph_k[5] = {0x7f, 0x08, 0x14, 0x22, 0x41};
    static const uint8_t glyph_l[5] = {0x7f, 0x40, 0x40, 0x40, 0x40};
    static const uint8_t glyph_m[5] = {0x7f, 0x02, 0x0c, 0x02, 0x7f};
    static const uint8_t glyph_n[5] = {0x7f, 0x04, 0x08, 0x10, 0x7f};
    static const uint8_t glyph_o[5] = {0x3e, 0x41, 0x41, 0x41, 0x3e};
    static const uint8_t glyph_p[5] = {0x7f, 0x09, 0x09, 0x09, 0x06};
    static const uint8_t glyph_q[5] = {0x3e, 0x41, 0x51, 0x21, 0x5e};
    static const uint8_t glyph_r[5] = {0x7f, 0x09, 0x19, 0x29, 0x46};
    static const uint8_t glyph_s[5] = {0x46, 0x49, 0x49, 0x49, 0x31};
    static const uint8_t glyph_t[5] = {0x01, 0x01, 0x7f, 0x01, 0x01};
    static const uint8_t glyph_u[5] = {0x3f, 0x40, 0x40, 0x40, 0x3f};
    static const uint8_t glyph_v[5] = {0x1f, 0x20, 0x40, 0x20, 0x1f};
    static const uint8_t glyph_w[5] = {0x3f, 0x40, 0x38, 0x40, 0x3f};
    static const uint8_t glyph_x[5] = {0x63, 0x14, 0x08, 0x14, 0x63};
    static const uint8_t glyph_y[5] = {0x07, 0x08, 0x70, 0x08, 0x07};
    static const uint8_t glyph_z[5] = {0x61, 0x51, 0x49, 0x45, 0x43};
    static const uint8_t glyph_dot[5] = {0x00, 0x60, 0x60, 0x00, 0x00};
    static const uint8_t glyph_comma[5] = {0x00, 0x80, 0x60, 0x00, 0x00};
    static const uint8_t glyph_bang[5] = {0x00, 0x00, 0x5f, 0x00, 0x00};
    static const uint8_t glyph_question[5] = {0x02, 0x01, 0x51, 0x09, 0x06};
    static const uint8_t glyph_colon[5] = {0x00, 0x36, 0x36, 0x00, 0x00};
    static const uint8_t glyph_dash[5] = {0x08, 0x08, 0x08, 0x08, 0x08};
    static const uint8_t glyph_plus[5] = {0x08, 0x08, 0x3e, 0x08, 0x08};
    static const uint8_t glyph_quote[5] = {0x00, 0x07, 0x00, 0x07, 0x00};
    static const uint8_t glyph_apostrophe[5] = {0x00, 0x00, 0x07, 0x00, 0x00};

    if (c >= 'a' && c <= 'z') {
        c = (char)(c - 'a' + 'A');
    }
    switch (c) {
    case '0': return glyph_0;
    case '1': return glyph_1;
    case '2': return glyph_2;
    case '3': return glyph_3;
    case '4': return glyph_4;
    case '5': return glyph_5;
    case '6': return glyph_6;
    case '7': return glyph_7;
    case '8': return glyph_8;
    case '9': return glyph_9;
    case 'A': return glyph_a;
    case 'B': return glyph_b;
    case 'C': return glyph_c;
    case 'D': return glyph_d;
    case 'E': return glyph_e;
    case 'F': return glyph_f;
    case 'G': return glyph_g;
    case 'H': return glyph_h;
    case 'I': return glyph_i;
    case 'J': return glyph_j;
    case 'K': return glyph_k;
    case 'L': return glyph_l;
    case 'M': return glyph_m;
    case 'N': return glyph_n;
    case 'O': return glyph_o;
    case 'P': return glyph_p;
    case 'Q': return glyph_q;
    case 'R': return glyph_r;
    case 'S': return glyph_s;
    case 'T': return glyph_t;
    case 'U': return glyph_u;
    case 'V': return glyph_v;
    case 'W': return glyph_w;
    case 'X': return glyph_x;
    case 'Y': return glyph_y;
    case 'Z': return glyph_z;
    case '.': return glyph_dot;
    case ',': return glyph_comma;
    case '!': return glyph_bang;
    case '?': return glyph_question;
    case ':': return glyph_colon;
    case '-': return glyph_dash;
    case '+': return glyph_plus;
    case '"': return glyph_quote;
    case '\'': return glyph_apostrophe;
    default: return blank;
    }
}

static void draw_circle(esp_lcd_panel_handle_t panel, int center_x, int center_y, int radius, uint16_t color)
{
    const int diameter = radius * 2 + 1;
    const uint16_t background = rgb565(3, 6, 12);
    uint16_t *buffer = heap_caps_malloc(diameter * diameter * sizeof(uint16_t), MALLOC_CAP_DMA);

    ESP_ERROR_CHECK(buffer ? ESP_OK : ESP_ERR_NO_MEM);

    const int x0 = (center_x - radius < 0) ? 0 : center_x - radius;
    const int y0 = (center_y - radius < 0) ? 0 : center_y - radius;
    const int x1 = (center_x + radius + 1 > LCD_H_RES) ? LCD_H_RES : center_x + radius + 1;
    const int y1 = (center_y + radius + 1 > LCD_V_RES) ? LCD_V_RES : center_y + radius + 1;
    const int width = x1 - x0;
    const int height = y1 - y0;

    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            const int px = x0 + x - center_x;
            const int py = y0 + y - center_y;
            buffer[y * width + x] = (px * px + py * py <= radius * radius) ? color : background;
        }
    }

    ESP_ERROR_CHECK(esp_lcd_panel_draw_bitmap(panel, x0, y0, x1, y1, buffer));
    heap_caps_free(buffer);
}

static void draw_alert_circle(esp_lcd_panel_handle_t panel)
{
    const int radius = 92;
    const int center_x = LCD_H_RES / 2;
    const int center_y = LCD_V_RES / 2;
    const uint16_t circle = rgb565(225, 52, 60);
    const uint16_t mark = rgb565(255, 246, 232);
    uint16_t *buffer = heap_caps_malloc((radius * 2) * sizeof(uint16_t), MALLOC_CAP_DMA);

    ESP_ERROR_CHECK(buffer ? ESP_OK : ESP_ERR_NO_MEM);

    for (int y = -radius; y < radius; ++y) {
        int half_width = 0;
        while ((half_width + 1) * (half_width + 1) + y * y <= radius * radius) {
            half_width++;
        }

        fill_rect(panel, buffer,
                  center_x - half_width,
                  center_y + y,
                  center_x + half_width,
                  center_y + y + 1,
                  circle);
    }
    heap_caps_free(buffer);

    buffer = heap_caps_malloc(24 * 96 * sizeof(uint16_t), MALLOC_CAP_DMA);
    ESP_ERROR_CHECK(buffer ? ESP_OK : ESP_ERR_NO_MEM);
    fill_rect(panel, buffer, center_x - 8, center_y - 40, center_x + 8, center_y + 24, mark);
    fill_rect(panel, buffer, center_x - 9, center_y + 48, center_x + 9, center_y + 66, mark);
    heap_caps_free(buffer);
}

static bool touch_pressed(esp_lcd_touch_handle_t touch)
{
    uint16_t x[1] = {0};
    uint16_t y[1] = {0};
    uint16_t strength[1] = {0};
    uint8_t count = 0;
    esp_err_t ret;

    if (touch == NULL) {
        return false;
    }
    if (gpio_get_level(TOUCH_PIN_INT) != 0) {
        return false;
    }

    ret = esp_lcd_touch_read_data(touch);
    if (ret != ESP_OK) {
        return false;
    }
    return esp_lcd_touch_get_coordinates(touch, x, y, strength, &count, 1) && count > 0;
}

static void draw_hline(esp_lcd_panel_handle_t panel, uint16_t *buffer, int x0, int x1, int y, uint16_t color)
{
    fill_rect(panel, buffer, x0, y, x1, y + 2, color);
}

static void draw_vline(esp_lcd_panel_handle_t panel, uint16_t *buffer, int x, int y0, int y1, uint16_t color)
{
    fill_rect(panel, buffer, x, y0, x + 2, y1, color);
}

static void draw_imu_background(esp_lcd_panel_handle_t panel)
{
    const int max_block_height = 32;
    uint16_t *buffer = heap_caps_malloc(LCD_H_RES * max_block_height * sizeof(uint16_t), MALLOC_CAP_DMA);

    ESP_ERROR_CHECK(buffer ? ESP_OK : ESP_ERR_NO_MEM);

    const uint16_t background = rgb565(2, 5, 10);
    const uint16_t grid = rgb565(18, 28, 42);
    const uint16_t axis = rgb565(80, 105, 130);

    for (int y = 0; y < LCD_V_RES; y += max_block_height) {
        const int block_end = (y + max_block_height < LCD_V_RES) ? y + max_block_height : LCD_V_RES;
        fill_rect(panel, buffer, 0, y, LCD_H_RES, block_end, background);
    }

    for (int y = 32; y < 288; y += 32) {
        draw_hline(panel, buffer, 24, LCD_H_RES - 24, y, grid);
    }
    for (int x = 24; x < LCD_H_RES - 24; x += 32) {
        draw_vline(panel, buffer, x, 32, 288, grid);
    }

    draw_hline(panel, buffer, 24, LCD_H_RES - 24, 160, axis);
    draw_vline(panel, buffer, LCD_H_RES / 2, 32, 288, axis);

    fill_rect(panel, buffer, 24, 315, LCD_H_RES - 24, 317, axis);
    fill_rect(panel, buffer, 24, 355, LCD_H_RES - 24, 357, axis);
    fill_rect(panel, buffer, 24, 395, LCD_H_RES - 24, 397, axis);

    heap_caps_free(buffer);
}

static void draw_signed_bar(esp_lcd_panel_handle_t panel, uint16_t *buffer, int y, float value, float range, uint16_t color)
{
    const int x0 = 24;
    const int x1 = LCD_H_RES - 24;
    const int center = (x0 + x1) / 2;
    const int half_width = (x1 - x0) / 2;
    const uint16_t background = rgb565(2, 5, 10);
    const uint16_t axis = rgb565(80, 105, 130);
    int bar = (int)(clamp_float(value / range, -1.0f, 1.0f) * half_width);

    fill_rect(panel, buffer, x0, y, x1, y + 18, background);
    fill_rect(panel, buffer, center - 1, y, center + 1, y + 18, axis);
    if (bar > 0) {
        fill_rect(panel, buffer, center, y + 3, center + bar, y + 15, color);
    } else if (bar < 0) {
        fill_rect(panel, buffer, center + bar, y + 3, center, y + 15, color);
    }
}

static void render_imu_sample(esp_lcd_panel_handle_t panel, const imu_sample_t *sample)
{
    static bool has_previous_dot = false;
    static int previous_x = LCD_H_RES / 2;
    static int previous_y = 160;
    const uint16_t background = rgb565(2, 5, 10);
    const uint16_t dot = rgb565(255, 92, 40);
    const uint16_t accel_x = rgb565(0, 180, 255);
    const uint16_t accel_y = rgb565(75, 220, 120);
    const uint16_t accel_z = rgb565(255, 210, 50);
    uint16_t *buffer = heap_caps_malloc(LCD_H_RES * 20 * sizeof(uint16_t), MALLOC_CAP_DMA);

    ESP_ERROR_CHECK(buffer ? ESP_OK : ESP_ERR_NO_MEM);

    if (has_previous_dot) {
        draw_circle(panel, previous_x, previous_y, 13, background);
    }

    const int x = clamp_int((LCD_H_RES / 2) + (int)(sample->ax_g * 105.0f), 24, LCD_H_RES - 24);
    const int y = clamp_int(160 - (int)(sample->ay_g * 105.0f), 32, 288);
    draw_circle(panel, x, y, 13, dot);
    previous_x = x;
    previous_y = y;
    has_previous_dot = true;

    draw_signed_bar(panel, buffer, 320, sample->ax_g, 1.2f, accel_x);
    draw_signed_bar(panel, buffer, 360, sample->ay_g, 1.2f, accel_y);
    draw_signed_bar(panel, buffer, 400, sample->az_g - 1.0f, 1.2f, accel_z);

    heap_caps_free(buffer);
}

static void draw_audio_background(esp_lcd_panel_handle_t panel)
{
    const int max_block_height = 32;
    uint16_t *buffer = heap_caps_malloc(LCD_H_RES * max_block_height * sizeof(uint16_t), MALLOC_CAP_DMA);

    ESP_ERROR_CHECK(buffer ? ESP_OK : ESP_ERR_NO_MEM);

    const uint16_t background = rgb565(5, 9, 12);
    const uint16_t panel_bg = rgb565(9, 16, 20);
    const uint16_t rule = rgb565(34, 54, 58);

    for (int y = 0; y < LCD_V_RES; y += max_block_height) {
        const int block_end = (y + max_block_height < LCD_V_RES) ? y + max_block_height : LCD_V_RES;
        fill_rect(panel, buffer, 0, y, LCD_H_RES, block_end, background);
    }

    fill_rect_chunked(panel, buffer, max_block_height, 0, UI_STATUS_H, LCD_H_RES, UI_STATUS_H + 2, rule);
    fill_rect_chunked(panel, buffer, max_block_height, UI_MARGIN, 34, LCD_H_RES - UI_MARGIN, 356, panel_bg);
    fill_rect_chunked(panel, buffer, max_block_height, UI_METER_X, UI_METER_Y, UI_METER_X + UI_METER_W, UI_METER_Y + UI_METER_H, rgb565(15, 28, 30));
    fill_rect_chunked(panel, buffer, max_block_height, UI_METER_X, UI_METER_Y + UI_METER_H + 8, UI_METER_X + UI_METER_W, UI_METER_Y + UI_METER_H + 10, rule);

    heap_caps_free(buffer);
}

static void render_audio_level(esp_lcd_panel_handle_t panel, float level)
{
    static float smoothed = 0.0f;
    const uint16_t background = rgb565(15, 28, 30);
    const uint16_t low = rgb565(50, 150, 140);
    const uint16_t high = rgb565(255, 196, 60);
    uint16_t *buffer = heap_caps_malloc(UI_METER_W * UI_METER_H * sizeof(uint16_t), MALLOC_CAP_DMA);

    ESP_ERROR_CHECK(buffer ? ESP_OK : ESP_ERR_NO_MEM);

    level = clamp_float(level, 0.0f, 1.0f);
    smoothed = (level > smoothed) ? (level * 0.65f + smoothed * 0.35f) : (level * 0.15f + smoothed * 0.85f);

    fill_rect(panel, buffer, UI_METER_X, UI_METER_Y, UI_METER_X + UI_METER_W, UI_METER_Y + UI_METER_H, background);

    const int bar_width = (int)(UI_METER_W * smoothed);
    const uint16_t color = (smoothed > 0.72f) ? high : low;
    fill_rect(panel, buffer, UI_METER_X, UI_METER_Y, UI_METER_X + bar_width, UI_METER_Y + UI_METER_H, color);

    heap_caps_free(buffer);
}

static void draw_char(esp_lcd_panel_handle_t panel, int x, int y, char c, uint16_t fg, uint16_t bg)
{
    uint16_t *buffer = heap_caps_malloc(UI_CHAR_W * UI_LINE_H * sizeof(uint16_t), MALLOC_CAP_DMA);
    const uint8_t *glyph = font5x7(c);

    ESP_ERROR_CHECK(buffer ? ESP_OK : ESP_ERR_NO_MEM);

    for (int py = 0; py < UI_LINE_H; ++py) {
        for (int px = 0; px < UI_CHAR_W; ++px) {
            bool on = false;
            const int gx = px / UI_FONT_SCALE;
            const int gy = py / UI_FONT_SCALE;
            if (gx < UI_FONT_W && gy < UI_FONT_H) {
                on = (glyph[gx] & (1U << gy)) != 0;
            }
            buffer[py * UI_CHAR_W + px] = on ? fg : bg;
        }
    }

    ESP_ERROR_CHECK(esp_lcd_panel_draw_bitmap(panel, x, y, x + UI_CHAR_W, y + UI_LINE_H, buffer));
    heap_caps_free(buffer);
}

static void render_wrapped_text(esp_lcd_panel_handle_t panel, const char *text)
{
    const uint16_t fg = rgb565(215, 232, 226);
    const uint16_t bg = rgb565(9, 16, 20);
    uint16_t *buffer = heap_caps_malloc(LCD_H_RES * 24 * sizeof(uint16_t), MALLOC_CAP_DMA);
    int x = UI_TEXT_X;
    int y = UI_TEXT_Y;
    int line_chars = 0;
    const int max_chars = UI_TEXT_W / UI_CHAR_W;

    ESP_ERROR_CHECK(buffer ? ESP_OK : ESP_ERR_NO_MEM);
    fill_rect_chunked(panel, buffer, 24, UI_TEXT_X, UI_TEXT_Y, UI_TEXT_X + UI_TEXT_W, UI_TEXT_Y + UI_TEXT_H, bg);
    heap_caps_free(buffer);

    for (size_t i = 0; text[i] != '\0' && y + UI_LINE_H <= UI_TEXT_Y + UI_TEXT_H; ++i) {
        char c = text[i];
        if (c == '\n' || c == '\r') {
            x = UI_TEXT_X;
            y += UI_LINE_H;
            line_chars = 0;
            continue;
        }
        if (c == ' ' && line_chars == 0) {
            continue;
        }
        if (line_chars >= max_chars) {
            x = UI_TEXT_X;
            y += UI_LINE_H;
            line_chars = 0;
            if (y + UI_LINE_H > UI_TEXT_Y + UI_TEXT_H || c == ' ') {
                continue;
            }
        }
        draw_char(panel, x, y, c, fg, bg);
        x += UI_CHAR_W;
        line_chars++;
    }
}

static void render_command_screen(esp_lcd_panel_handle_t panel)
{
    draw_audio_background(panel);
    render_wrapped_text(panel, s_last_command_text);
    render_power_status(panel);
}

static void render_power_status(esp_lcd_panel_handle_t panel)
{
    power_status_t status = {0};
    uint16_t *buffer = heap_caps_malloc(118 * 20 * sizeof(uint16_t), MALLOC_CAP_DMA);
    const uint16_t bg = rgb565(5, 9, 12);
    const uint16_t border = rgb565(86, 112, 112);
    const uint16_t fill_ok = rgb565(60, 185, 130);
    const uint16_t fill_low = rgb565(220, 70, 70);
    const uint16_t fill_charge = rgb565(255, 196, 60);
    const uint16_t text = rgb565(190, 215, 210);
    const int x = LCD_H_RES - 126;
    const int y = 24;
    char label[8] = "--";

    ESP_ERROR_CHECK(buffer ? ESP_OK : ESP_ERR_NO_MEM);
    fill_rect(panel, buffer, x, y, x + 118, y + 20, bg);
    heap_caps_free(buffer);

    if (axp2101_read_power_status(&status) != ESP_OK || !status.valid) {
        draw_char(panel, x + 56, y + 1, '?', text, bg);
        return;
    }

    if (status.battery_present && status.percent >= 0) {
        int display_percent = clamp_int(status.percent, 0, 100);
        snprintf(label, sizeof(label), "%d%%", display_percent);
    } else if (status.vbus_present) {
        snprintf(label, sizeof(label), "USB");
    } else {
        snprintf(label, sizeof(label), "BAT");
    }

    uint16_t *icon = heap_caps_malloc(34 * 14 * sizeof(uint16_t), MALLOC_CAP_DMA);
    ESP_ERROR_CHECK(icon ? ESP_OK : ESP_ERR_NO_MEM);
    fill_rect(panel, icon, x, y + 3, x + 30, y + 17, bg);
    fill_rect(panel, icon, x, y + 3, x + 28, y + 5, border);
    fill_rect(panel, icon, x, y + 15, x + 28, y + 17, border);
    fill_rect(panel, icon, x, y + 3, x + 2, y + 17, border);
    fill_rect(panel, icon, x + 26, y + 3, x + 28, y + 17, border);
    fill_rect(panel, icon, x + 28, y + 7, x + 32, y + 13, border);

    if (status.battery_present && status.percent >= 0) {
        int fill_w = clamp_int((status.percent * 22) / 100, 1, 22);
        fill_rect(panel, icon, x + 4, y + 7, x + 4 + fill_w, y + 13,
                  status.charging ? fill_charge : (status.percent <= 20 ? fill_low : fill_ok));
    } else if (status.vbus_present) {
        fill_rect(panel, icon, x + 8, y + 7, x + 20, y + 13, fill_charge);
    }
    heap_caps_free(icon);

    for (size_t i = 0; label[i] != '\0' && i < 5; ++i) {
        draw_char(panel, x + 40 + (int)i * UI_CHAR_W, y + 1, label[i], text, bg);
    }

    if (status.charging || status.vbus_present) {
        draw_char(panel, x + 92, y + 1, '+', fill_charge, bg);
    }
}

static void render_status_band(esp_lcd_panel_handle_t panel, uint16_t color)
{
    uint16_t *buffer = heap_caps_malloc(LCD_H_RES * 18 * sizeof(uint16_t), MALLOC_CAP_DMA);
    ESP_ERROR_CHECK(buffer ? ESP_OK : ESP_ERR_NO_MEM);
    fill_rect(panel, buffer, 0, 0, LCD_H_RES, 18, color);
    heap_caps_free(buffer);
}

static void render_recording_band(esp_lcd_panel_handle_t panel, size_t captured_samples)
{
    uint16_t *buffer = heap_caps_malloc(LCD_H_RES * 18 * sizeof(uint16_t), MALLOC_CAP_DMA);
    const uint16_t background = rgb565(80, 45, 8);
    const uint16_t recording = rgb565(255, 196, 60);
    const int width = clamp_int((int)((captured_samples * LCD_H_RES) / COMMAND_CAPTURE_SAMPLES), 4, LCD_H_RES);

    ESP_ERROR_CHECK(buffer ? ESP_OK : ESP_ERR_NO_MEM);
    fill_rect(panel, buffer, 0, 0, LCD_H_RES, 18, background);
    fill_rect(panel, buffer, 0, 0, width, 18, recording);
    heap_caps_free(buffer);
}

static float audio_level_from_samples(const int16_t *samples, size_t sample_count)
{
    int64_t sum = 0;
    int64_t magnitude = 0;

    if (sample_count == 0) {
        return 0.0f;
    }

    for (size_t i = 0; i < sample_count; ++i) {
        sum += samples[i];
    }

    const int32_t dc = (int32_t)(sum / (int64_t)sample_count);
    for (size_t i = 0; i < sample_count; i += 2) {
        int32_t centered = (int32_t)samples[i] - dc;
        magnitude += (centered < 0) ? -centered : centered;
    }

    float level = ((float)magnitude / (float)(sample_count / 2)) / 32768.0f;
    return clamp_float(level * AUDIO_VISUAL_GAIN, 0.0f, 1.0f);
}

static esp_err_t es8311_codec_init(void)
{
    if (s_es8311 == NULL) {
        s_es8311 = es8311_create(I2C_PORT, ES8311_ADDRESS_0);
        ESP_RETURN_ON_FALSE(s_es8311, ESP_FAIL, TAG, "create ES8311");
    }

    const es8311_clock_config_t es_clk = {
        .mclk_inverted = false,
        .sclk_inverted = false,
        .mclk_from_mclk_pin = true,
        .mclk_frequency = AUDIO_SAMPLE_RATE * 256,
        .sample_frequency = AUDIO_SAMPLE_RATE,
    };

    ESP_LOGI(TAG, "Initialize ES8311 codec");
    ESP_RETURN_ON_ERROR(es8311_init(s_es8311, &es_clk, ES8311_RESOLUTION_16, ES8311_RESOLUTION_16), TAG, "init ES8311");
    ESP_RETURN_ON_ERROR(es8311_sample_frequency_config(s_es8311, es_clk.mclk_frequency, es_clk.sample_frequency), TAG, "set ES8311 sample rate");
    ESP_RETURN_ON_ERROR(es8311_microphone_config(s_es8311, false), TAG, "configure ES8311 microphone");
    ESP_RETURN_ON_ERROR(es8311_voice_volume_set(s_es8311, ES8311_VOICE_VOLUME, NULL), TAG, "set ES8311 volume");
    ESP_RETURN_ON_ERROR(es8311_microphone_gain_set(s_es8311, ES8311_MIC_GAIN), TAG, "set ES8311 mic gain");

    return ESP_OK;
}

static esp_err_t i2s_audio_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_PORT, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;

    ESP_LOGI(TAG, "Initialize I2S audio bus");
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, &s_i2s_tx, &s_i2s_rx), TAG, "create I2S channels");

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(AUDIO_SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_PIN_MCLK,
            .bclk = I2S_PIN_BCLK,
            .ws = I2S_PIN_WS,
            .dout = I2S_PIN_DOUT,
            .din = I2S_PIN_DIN,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };
    std_cfg.clk_cfg.mclk_multiple = I2S_MCLK_MULTIPLE_256;

    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(s_i2s_tx, &std_cfg), TAG, "init I2S TX");
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(s_i2s_rx, &std_cfg), TAG, "init I2S RX");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(s_i2s_tx), TAG, "enable I2S TX");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(s_i2s_rx), TAG, "enable I2S RX");

    return ESP_OK;
}

typedef struct {
    char data[COMMAND_HTTP_RESPONSE_MAX];
    int length;
} command_http_response_t;

static esp_err_t command_http_event_handler(esp_http_client_event_t *event)
{
    if (event->event_id == HTTP_EVENT_ON_DATA && event->user_data != NULL) {
        command_http_response_t *response = (command_http_response_t *)event->user_data;
        int remaining = sizeof(response->data) - response->length - 1;
        if (remaining > 0) {
            int copy_len = event->data_len < remaining ? event->data_len : remaining;
            memcpy(response->data + response->length, event->data, copy_len);
            response->length += copy_len;
            response->data[response->length] = '\0';
        }
    }
    return ESP_OK;
}

static bool extract_json_string_field(const char *json, const char *field, char *out, size_t out_size)
{
    char pattern[32] = {0};
    const char *cursor = NULL;
    size_t written = 0;

    if (out_size == 0) {
        return false;
    }
    out[0] = '\0';
    snprintf(pattern, sizeof(pattern), "\"%s\"", field);
    cursor = strstr(json, pattern);
    if (cursor == NULL) {
        return false;
    }
    cursor = strchr(cursor + strlen(pattern), ':');
    if (cursor == NULL) {
        return false;
    }
    cursor = strchr(cursor, '"');
    if (cursor == NULL) {
        return false;
    }
    cursor++;

    while (*cursor != '\0' && *cursor != '"' && written + 1 < out_size) {
        if (*cursor == '\\' && cursor[1] != '\0') {
            cursor++;
            switch (*cursor) {
            case 'n':
                out[written++] = '\n';
                break;
            case 'r':
            case 't':
                out[written++] = ' ';
                break;
            default:
                out[written++] = *cursor;
                break;
            }
        } else {
            out[written++] = *cursor;
        }
        cursor++;
    }
    out[written] = '\0';
    return written > 0;
}

static esp_err_t play_square_tone(uint16_t frequency_hz, uint16_t duration_ms, int16_t amplitude)
{
    const int total_frames = (AUDIO_SAMPLE_RATE * duration_ms) / 1000;
    const int half_period_frames = AUDIO_SAMPLE_RATE / (frequency_hz * 2);
    int frame = 0;

    ESP_RETURN_ON_FALSE(s_i2s_tx != NULL, ESP_ERR_INVALID_STATE, TAG, "I2S TX is unavailable");
    ESP_RETURN_ON_FALSE(half_period_frames > 0, ESP_ERR_INVALID_ARG, TAG, "invalid tone frequency");

    while (frame < total_frames) {
        const int frames_this_chunk = (total_frames - frame) > TONE_CHUNK_FRAMES ? TONE_CHUNK_FRAMES : (total_frames - frame);
        for (int i = 0; i < frames_this_chunk; ++i) {
            int16_t sample = (((frame + i) / half_period_frames) % 2) == 0 ? amplitude : -amplitude;
            s_tone_chunk[i * 2] = sample;
            s_tone_chunk[i * 2 + 1] = sample;
        }

        size_t bytes_written = 0;
        esp_err_t ret = i2s_channel_write(s_i2s_tx, s_tone_chunk, frames_this_chunk * 2 * sizeof(int16_t), &bytes_written, pdMS_TO_TICKS(1000));
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "Tone write skipped: %s", esp_err_to_name(ret));
            return ret;
        }
        frame += frames_this_chunk;
    }
    return ESP_OK;
}

static void play_silence(uint16_t duration_ms)
{
    const int total_frames = (AUDIO_SAMPLE_RATE * duration_ms) / 1000;
    int frame = 0;

    memset(s_tone_chunk, 0, sizeof(s_tone_chunk));
    while (frame < total_frames) {
        const int frames_this_chunk = (total_frames - frame) > TONE_CHUNK_FRAMES ? TONE_CHUNK_FRAMES : (total_frames - frame);
        size_t bytes_written = 0;
        if (i2s_channel_write(s_i2s_tx, s_tone_chunk, frames_this_chunk * 2 * sizeof(int16_t), &bytes_written, pdMS_TO_TICKS(1000)) != ESP_OK) {
            return;
        }
        frame += frames_this_chunk;
    }
}

static void play_device_tone(const char *tone)
{
    if (tone == NULL || tone[0] == '\0' || strcmp(tone, "none") == 0) {
        return;
    }

    if (strcmp(tone, "success") == 0) {
        (void)play_square_tone(880, 70, 5000);
        play_silence(35);
        (void)play_square_tone(1320, 90, 5000);
        return;
    }

    if (strcmp(tone, "error") == 0) {
        (void)play_square_tone(220, 180, 5500);
        return;
    }

    if (strcmp(tone, "alert") == 0) {
        for (int i = 0; i < 3; ++i) {
            (void)play_square_tone(1040, 85, 6200);
            play_silence(55);
        }
        return;
    }

    ESP_LOGW(TAG, "Unknown response tone: %s", tone);
}

static void build_device_events_url(char *url, size_t url_size)
{
    strlcpy(url, CONFIG_SPOKEN_COMMAND_SERVER_URL, url_size);
    char *path = strstr(url, "/audio/command");
    if (path != NULL) {
        *path = '\0';
    }
    size_t used = strlen(url);
    snprintf(url + used, url_size - used, "/devices/%s/events", s_device_id);
}

static void build_device_register_url(char *url, size_t url_size)
{
    strlcpy(url, CONFIG_SPOKEN_COMMAND_SERVER_URL, url_size);
    char *path = strstr(url, "/audio/command");
    if (path != NULL) {
        *path = '\0';
    }
    size_t used = strlen(url);
    snprintf(url + used, url_size - used, "/devices/%s/register", s_device_id);
}

static esp_err_t register_device_metadata(void)
{
    if (!s_wifi_ready) {
        return ESP_ERR_INVALID_STATE;
    }

    char url[DEVICE_REGISTER_URL_MAX] = {0};
    build_device_register_url(url, sizeof(url));

    const char *body =
        "{"
        "\"type\":\"voice-controller\","
        "\"model\":\"Waveshare ESP32-C6 Touch AMOLED 1.8\","
        "\"capabilities\":[\"microphone\",\"display\",\"touch\",\"speaker\",\"button\",\"imu\",\"battery\",\"command-audio\",\"device-events\"],"
        "\"status\":{"
        "\"target\":\"" CONFIG_IDF_TARGET "\","
        "\"display\":\"SH8601 AMOLED 368x448\","
        "\"microphone\":\"ES8311/I2S\","
        "\"imu\":\"QMI8658\","
        "\"power\":\"AXP2101\""
        "}"
        "}";

    esp_http_client_config_t config = {
        .url = url,
        .timeout_ms = 3000,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    ESP_RETURN_ON_FALSE(client != NULL, ESP_ERR_NO_MEM, TAG, "create registration HTTP client");

    ESP_LOGI(TAG, "Registering device metadata at %s", url);
    ESP_RETURN_ON_ERROR(esp_http_client_set_method(client, HTTP_METHOD_POST), TAG, "set registration method");
    ESP_RETURN_ON_ERROR(esp_http_client_set_header(client, "Content-Type", "application/json"), TAG, "set registration content type");
    ESP_RETURN_ON_ERROR(esp_http_client_set_post_field(client, body, strlen(body)), TAG, "set registration body");

    esp_err_t ret = esp_http_client_perform(client);
    if (ret == ESP_OK) {
        int status = esp_http_client_get_status_code(client);
        ESP_LOGI(TAG, "Device metadata registration status=%d", status);
        if (status < 200 || status >= 300) {
            ret = ESP_FAIL;
        }
    } else {
        ESP_LOGW(TAG, "Device metadata registration failed: %s", esp_err_to_name(ret));
    }
    esp_http_client_cleanup(client);
    return ret;
}

static void render_alert_event(esp_lcd_panel_handle_t panel, esp_lcd_touch_handle_t touch,
                               const char *display_text, const char *tone)
{
    const int64_t alert_until_us = esp_timer_get_time() + ALERT_DISPLAY_TIME_US;
    bool touch_was_pressed;

    strlcpy(s_last_command_text, display_text && display_text[0] ? display_text : "Alert", sizeof(s_last_command_text));
    render_command_screen(panel);
    draw_alert_circle(panel);
    play_device_tone(tone && tone[0] ? tone : "alert");

    touch_was_pressed = touch_pressed(touch);
    while (esp_timer_get_time() < alert_until_us) {
        const bool touch_is_pressed = touch_pressed(touch);
        if (touch_is_pressed && !touch_was_pressed) {
            break;
        }
        touch_was_pressed = touch_is_pressed;
        vTaskDelay(pdMS_TO_TICKS(50));
    }

    render_command_screen(panel);
}

static esp_err_t poll_device_events(esp_lcd_panel_handle_t panel, esp_lcd_touch_handle_t touch)
{
    if (!s_wifi_ready) {
        return ESP_ERR_INVALID_STATE;
    }

    char url[DEVICE_EVENTS_URL_MAX] = {0};
    command_http_response_t response = {0};
    build_device_events_url(url, sizeof(url));

    esp_http_client_config_t config = {
        .url = url,
        .timeout_ms = 2500,
        .event_handler = command_http_event_handler,
        .user_data = &response,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    ESP_RETURN_ON_FALSE(client != NULL, ESP_ERR_NO_MEM, TAG, "create events HTTP client");

    esp_err_t ret = esp_http_client_perform(client);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "Device event poll failed: %s", esp_err_to_name(ret));
        esp_http_client_cleanup(client);
        return ret;
    }

    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    if (status < 200 || status >= 300) {
        ESP_LOGW(TAG, "Device event poll status=%d", status);
        return ESP_FAIL;
    }

    if (strstr(response.data, "\"type\"") == NULL) {
        return ESP_OK;
    }

    char event_type[16] = {0};
    char display_text[COMMAND_TEXT_MAX] = {0};
    char tone[16] = {0};
    if (!extract_json_string_field(response.data, "type", event_type, sizeof(event_type))) {
        return ESP_OK;
    }
    extract_json_string_field(response.data, "display_text", display_text, sizeof(display_text));
    extract_json_string_field(response.data, "tone", tone, sizeof(tone));

    ESP_LOGI(TAG, "Device event type=%s display=%s tone=%s", event_type, display_text, tone);
    if (strcmp(event_type, "alert") == 0) {
        render_alert_event(panel, touch, display_text, tone);
    }
    return ESP_OK;
}

static esp_err_t write_all_http(esp_http_client_handle_t client, const char *data, int length)
{
    int written = 0;
    while (written < length) {
        int ret = esp_http_client_write(client, data + written, length - written);
        if (ret <= 0) {
            return ESP_ERR_HTTP_WRITE_DATA;
        }
        written += ret;
    }
    return ESP_OK;
}

static esp_err_t write_http_chunk(esp_http_client_handle_t client, const char *data, int length)
{
    char header[HTTP_CHUNK_HEADER_MAX] = {0};
    int header_len = snprintf(header, sizeof(header), "%x\r\n", length);
    ESP_RETURN_ON_FALSE(header_len > 0 && header_len < sizeof(header), ESP_ERR_INVALID_SIZE, TAG, "format chunk header");
    ESP_RETURN_ON_ERROR(write_all_http(client, header, header_len), TAG, "write chunk header");
    ESP_RETURN_ON_ERROR(write_all_http(client, data, length), TAG, "write chunk body");
    return write_all_http(client, "\r\n", 2);
}

static esp_err_t stream_command_audio(esp_lcd_panel_handle_t panel)
{
    command_http_response_t response = {0};
    size_t captured = 0;
    size_t last_rendered_samples = 0;
    esp_err_t ret = ESP_OK;

    if (!s_wifi_ready) {
        ESP_LOGW(TAG, "Command upload skipped: Wi-Fi is not connected");
        render_status_band(panel, rgb565(180, 50, 60));
        return ESP_ERR_INVALID_STATE;
    }

    ESP_LOGI(TAG, "Streaming command audio while BOOT is held, max %d ms", CONFIG_SPOKEN_COMMAND_CAPTURE_MS);

    esp_http_client_config_t config = {
        .url = CONFIG_SPOKEN_COMMAND_SERVER_URL,
        .timeout_ms = 30000,
        .event_handler = command_http_event_handler,
        .user_data = &response,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    ESP_RETURN_ON_FALSE(client != NULL, ESP_ERR_NO_MEM, TAG, "create HTTP client");

    ESP_GOTO_ON_ERROR(esp_http_client_set_method(client, HTTP_METHOD_POST), cleanup, TAG, "set HTTP method");
    ESP_GOTO_ON_ERROR(esp_http_client_set_header(client, "Content-Type", "application/octet-stream"), cleanup, TAG, "set content type");
    ESP_GOTO_ON_ERROR(esp_http_client_set_header(client, "X-Audio-Sample-Rate", "16000"), cleanup, TAG, "set sample rate");
    ESP_GOTO_ON_ERROR(esp_http_client_set_header(client, "X-Audio-Channels", "1"), cleanup, TAG, "set channels");
    ESP_GOTO_ON_ERROR(esp_http_client_set_header(client, "X-Device-Id", s_device_id), cleanup, TAG, "set device id");
    ESP_GOTO_ON_ERROR(esp_http_client_open(client, -1), cleanup, TAG, "open chunked HTTP stream");

    render_recording_band(panel, captured);

    while (captured < COMMAND_CAPTURE_SAMPLES && boot_button_is_held()) {
        size_t bytes_read = 0;
        ret = i2s_channel_read(s_i2s_rx, s_audio_samples, sizeof(s_audio_samples), &bytes_read, pdMS_TO_TICKS(1000));
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "Command capture read failed: %s", esp_err_to_name(ret));
            break;
        }

        const size_t sample_count = bytes_read / sizeof(s_audio_samples[0]);
        size_t mono_count = 0;
        for (size_t i = 0; i + 1 < sample_count && captured < COMMAND_CAPTURE_SAMPLES && mono_count < AUDIO_RECV_SAMPLES; i += 2) {
            s_command_chunk[mono_count++] = s_audio_samples[i];
            captured++;
        }

        if (mono_count > 0) {
            ESP_GOTO_ON_ERROR(write_http_chunk(client, (const char *)s_command_chunk, mono_count * sizeof(s_command_chunk[0])), cleanup, TAG, "write audio chunk");
        }

        if (captured - last_rendered_samples >= AUDIO_RECV_SAMPLES || captured == COMMAND_CAPTURE_SAMPLES) {
            render_recording_band(panel, captured);
            last_rendered_samples = captured;
        }
    }

    if (captured == 0) {
        ESP_LOGW(TAG, "Command upload skipped: no audio captured");
        render_status_band(panel, rgb565(180, 50, 60));
        ret = ESP_ERR_INVALID_SIZE;
        goto cleanup;
    }

    ESP_LOGI(TAG, "Finished streaming command audio: %u samples", (unsigned int)captured);
    render_status_band(panel, rgb565(0, 150, 220));
    ESP_GOTO_ON_ERROR(write_all_http(client, "0\r\n\r\n", 5), cleanup, TAG, "finish chunked HTTP stream");

    int64_t content_length = esp_http_client_fetch_headers(client);
    if (content_length >= 0 || esp_http_client_get_status_code(client) > 0) {
        int read_len = esp_http_client_read_response(client, response.data, sizeof(response.data) - 1);
        if (read_len >= 0) {
            response.length = read_len;
            response.data[read_len] = '\0';
        }
        int status = esp_http_client_get_status_code(client);
        ESP_LOGI(TAG, "Command server status=%d response=%s", status, response.data);
        render_status_band(panel, status >= 200 && status < 300 ? rgb565(40, 185, 145) : rgb565(180, 50, 60));
        if (status >= 200 && status < 300) {
            char tone[16] = {0};
            if (!extract_json_string_field(response.data, "display_text", s_last_command_text, sizeof(s_last_command_text)) &&
                !extract_json_string_field(response.data, "transcript", s_last_command_text, sizeof(s_last_command_text)) &&
                !extract_json_string_field(response.data, "text", s_last_command_text, sizeof(s_last_command_text))) {
                strlcpy(s_last_command_text, "Command received.", sizeof(s_last_command_text));
            }
            render_wrapped_text(panel, s_last_command_text);
            if (extract_json_string_field(response.data, "tone", tone, sizeof(tone))) {
                play_device_tone(tone);
            }
            ret = ESP_OK;
        } else {
            char tone[16] = {0};
            strlcpy(s_last_command_text, "Upload failed.", sizeof(s_last_command_text));
            extract_json_string_field(response.data, "display_text", s_last_command_text, sizeof(s_last_command_text));
            render_wrapped_text(panel, s_last_command_text);
            if (extract_json_string_field(response.data, "tone", tone, sizeof(tone))) {
                play_device_tone(tone);
            } else {
                play_device_tone("error");
            }
            ret = ESP_FAIL;
        }
    } else {
        ESP_LOGW(TAG, "Command upload failed while reading response");
        render_status_band(panel, rgb565(180, 50, 60));
        strlcpy(s_last_command_text, "No server response.", sizeof(s_last_command_text));
        render_wrapped_text(panel, s_last_command_text);
        ret = ESP_FAIL;
    }

cleanup:
    esp_http_client_cleanup(client);
    return ret;
}

static void run_command_interaction(esp_lcd_panel_handle_t panel)
{
    if (!s_visualizer_enabled) {
        ESP_ERROR_CHECK(set_visualizer_enabled(panel, true));
    }

    esp_err_t ret = stream_command_audio(panel);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "Command interaction incomplete: %s", esp_err_to_name(ret));
    }
    vTaskDelay(pdMS_TO_TICKS(500));
    render_command_screen(panel);
}

static void enter_power_off(esp_lcd_panel_handle_t panel)
{
    ESP_LOGI(TAG, "Power button pressed; entering PMU shutdown");
    strlcpy(s_last_command_text, "Powering off.", sizeof(s_last_command_text));
    draw_audio_background(panel);
    render_wrapped_text(panel, s_last_command_text);
    render_status_band(panel, rgb565(255, 196, 60));
    vTaskDelay(pdMS_TO_TICKS(250));

    if (s_i2s_rx != NULL) {
        ESP_ERROR_CHECK_WITHOUT_ABORT(i2s_channel_disable(s_i2s_rx));
    }
    if (s_i2s_tx != NULL) {
        ESP_ERROR_CHECK_WITHOUT_ABORT(i2s_channel_disable(s_i2s_tx));
    }
    ESP_ERROR_CHECK_WITHOUT_ABORT(set_mic_power(false));
    ESP_ERROR_CHECK_WITHOUT_ABORT(esp_lcd_panel_disp_on_off(panel, false));
    ESP_ERROR_CHECK_WITHOUT_ABORT(esp_wifi_stop());

    uint8_t adc_ctrl = 0;
    if (i2c_reg_read(AXP2101_ADDR, AXP2101_REG_ADC_CHANNEL_CTRL, &adc_ctrl, 1) == ESP_OK) {
        adc_ctrl &= ~(AXP2101_ADC_ENABLE_BATT | AXP2101_ADC_ENABLE_VBUS | AXP2101_ADC_ENABLE_VSYS);
        ESP_ERROR_CHECK_WITHOUT_ABORT(i2c_reg_write(AXP2101_ADDR, AXP2101_REG_ADC_CHANNEL_CTRL, adc_ctrl));
    }

    ESP_ERROR_CHECK_WITHOUT_ABORT(axp2101_shutdown());

    vTaskDelay(pdMS_TO_TICKS(1000));
    ESP_LOGW(TAG, "AXP2101 shutdown did not complete; falling back to ESP deep sleep. Use reset or a power cycle to wake.");
    fflush(stdout);
    esp_deep_sleep_start();
}

static esp_err_t set_visualizer_enabled(esp_lcd_panel_handle_t panel, bool enabled)
{
    if (s_visualizer_enabled == enabled) {
        return ESP_OK;
    }

    s_visualizer_enabled = enabled;
    if (!enabled) {
        ESP_LOGI(TAG, "Entering soft standby");
        if (s_i2s_rx != NULL) {
            ESP_RETURN_ON_ERROR(i2s_channel_disable(s_i2s_rx), TAG, "disable I2S RX");
        }
        if (s_i2s_tx != NULL) {
            ESP_RETURN_ON_ERROR(i2s_channel_disable(s_i2s_tx), TAG, "disable I2S TX");
        }
        ESP_RETURN_ON_ERROR(set_mic_power(false), TAG, "disable mic rail");
        ESP_RETURN_ON_ERROR(esp_lcd_panel_disp_on_off(panel, false), TAG, "turn display off");
        return ESP_OK;
    }

    ESP_LOGI(TAG, "Leaving soft standby");
    ESP_RETURN_ON_ERROR(set_mic_power(true), TAG, "enable mic rail");
    vTaskDelay(pdMS_TO_TICKS(30));
    ESP_RETURN_ON_ERROR(es8311_codec_init(), TAG, "restore ES8311 codec");
    if (s_i2s_tx != NULL) {
        ESP_RETURN_ON_ERROR(i2s_channel_enable(s_i2s_tx), TAG, "enable I2S TX");
    }
    if (s_i2s_rx != NULL) {
        ESP_RETURN_ON_ERROR(i2s_channel_enable(s_i2s_rx), TAG, "enable I2S RX");
    }
    ESP_RETURN_ON_ERROR(esp_lcd_panel_disp_on_off(panel, true), TAG, "turn display on");
    draw_audio_background(panel);

    return ESP_OK;
}

static int16_t le_i16(const uint8_t *data)
{
    return (int16_t)((uint16_t)data[1] << 8 | data[0]);
}

static esp_err_t qmi8658_init(void)
{
    uint8_t whoami = 0;
    uint8_t reset_result = 0;
    const uint8_t candidate_addrs[] = {QMI8658_ADDR_LOW, QMI8658_ADDR_HIGH};
    bool found = false;

    ESP_LOGI(TAG, "Initialize QMI8658 IMU");
    for (size_t i = 0; i < sizeof(candidate_addrs); ++i) {
        esp_err_t ret = i2c_reg_read(candidate_addrs[i], QMI8658_REG_WHOAMI, &whoami, 1);
        if (ret == ESP_OK) {
            ESP_LOGI(TAG, "QMI8658 probe addr=0x%02x WHOAMI=0x%02x", candidate_addrs[i], whoami);
            if (whoami == QMI8658_WHOAMI_VALUE) {
                s_qmi8658_addr = candidate_addrs[i];
                found = true;
                break;
            }
        } else {
            ESP_LOGI(TAG, "QMI8658 probe addr=0x%02x failed: %s", candidate_addrs[i], esp_err_to_name(ret));
        }
    }
    ESP_RETURN_ON_FALSE(found, ESP_ERR_NOT_FOUND, TAG, "QMI8658 not found");

    ESP_RETURN_ON_ERROR(i2c_reg_write(s_qmi8658_addr, QMI8658_REG_RESET, QMI8658_RESET_VALUE), TAG, "reset QMI8658");
    vTaskDelay(pdMS_TO_TICKS(20));
    ESP_RETURN_ON_ERROR(i2c_reg_read(s_qmi8658_addr, QMI8658_REG_RST_RESULT, &reset_result, 1), TAG, "read QMI8658 reset result");
    ESP_LOGI(TAG, "QMI8658 addr=0x%02x WHOAMI=0x%02x reset_result=0x%02x", s_qmi8658_addr, whoami, reset_result);

    ESP_RETURN_ON_ERROR(i2c_reg_update(s_qmi8658_addr, QMI8658_REG_CTRL1, (uint8_t)~BIT(6), BIT(6)), TAG, "enable QMI8658 auto-increment");
    ESP_RETURN_ON_ERROR(i2c_reg_write(s_qmi8658_addr, QMI8658_REG_CTRL2, 0x26), TAG, "configure QMI8658 accel");
    ESP_RETURN_ON_ERROR(i2c_reg_write(s_qmi8658_addr, QMI8658_REG_CTRL3, 0x55), TAG, "configure QMI8658 gyro");
    ESP_RETURN_ON_ERROR(i2c_reg_write(s_qmi8658_addr, QMI8658_REG_CTRL7, 0x03), TAG, "enable QMI8658 accel/gyro");
    vTaskDelay(pdMS_TO_TICKS(20));

    return ESP_OK;
}

static esp_err_t qmi8658_read(imu_sample_t *sample)
{
    uint8_t status = 0;
    uint8_t raw[14] = {0};

    ESP_RETURN_ON_ERROR(i2c_reg_read(s_qmi8658_addr, QMI8658_REG_STATUS0, &status, 1), TAG, "read QMI8658 status");
    ESP_RETURN_ON_FALSE((status & 0x03) != 0, ESP_ERR_INVALID_STATE, TAG, "QMI8658 data not ready");
    ESP_RETURN_ON_ERROR(i2c_reg_read(s_qmi8658_addr, QMI8658_REG_TEMP_L, raw, sizeof(raw)), TAG, "read QMI8658 data");

    sample->temperature_c = (float)raw[1] + ((float)raw[0] / 256.0f);
    sample->ax_g = le_i16(&raw[2]) * QMI8658_ACCEL_SCALE_G;
    sample->ay_g = le_i16(&raw[4]) * QMI8658_ACCEL_SCALE_G;
    sample->az_g = le_i16(&raw[6]) * QMI8658_ACCEL_SCALE_G;
    sample->gx_dps = le_i16(&raw[8]) * QMI8658_GYRO_SCALE_DPS;
    sample->gy_dps = le_i16(&raw[10]) * QMI8658_GYRO_SCALE_DPS;
    sample->gz_dps = le_i16(&raw[12]) * QMI8658_GYRO_SCALE_DPS;

    return ESP_OK;
}

static esp_lcd_touch_handle_t touch_init(void)
{
    esp_lcd_panel_io_handle_t touch_io = NULL;
    const esp_lcd_panel_io_i2c_config_t touch_io_config = ESP_LCD_TOUCH_IO_I2C_FT5x06_CONFIG();
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_i2c((esp_lcd_i2c_bus_handle_t)I2C_PORT, &touch_io_config, &touch_io));

    const esp_lcd_touch_config_t touch_config = {
        .x_max = LCD_H_RES,
        .y_max = LCD_V_RES,
        .rst_gpio_num = TOUCH_PIN_RST,
        .int_gpio_num = TOUCH_PIN_INT,
        .levels = {
            .reset = 0,
            .interrupt = 0,
        },
        .flags = {
            .swap_xy = 0,
            .mirror_x = 0,
            .mirror_y = 0,
        },
    };

    ESP_LOGI(TAG, "Initialize FT5x06 touch controller");
    esp_lcd_touch_handle_t touch = NULL;
    ESP_ERROR_CHECK(esp_lcd_touch_new_i2c_ft5x06(touch_io, &touch_config, &touch));
    return touch;
}

static app_display_t display_init(void)
{
    ESP_ERROR_CHECK(i2c_init());
    ESP_ERROR_CHECK(reset_lcd_and_touch());

    ESP_LOGI(TAG, "Initialize LCD QSPI bus");
    const spi_bus_config_t buscfg = SH8601_PANEL_BUS_QSPI_CONFIG(
        LCD_PIN_PCLK,
        LCD_PIN_DATA0,
        LCD_PIN_DATA1,
        LCD_PIN_DATA2,
        LCD_PIN_DATA3,
        LCD_H_RES * 32 * sizeof(uint16_t));
    ESP_ERROR_CHECK(spi_bus_initialize(LCD_HOST, &buscfg, SPI_DMA_CH_AUTO));

    ESP_LOGI(TAG, "Install LCD panel IO");
    esp_lcd_panel_io_handle_t io_handle = NULL;
    const esp_lcd_panel_io_spi_config_t io_config = SH8601_PANEL_IO_QSPI_CONFIG(LCD_PIN_CS, NULL, NULL);
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)LCD_HOST, &io_config, &io_handle));

    const sh8601_vendor_config_t vendor_config = {
        .init_cmds = lcd_init_cmds,
        .init_cmds_size = sizeof(lcd_init_cmds) / sizeof(lcd_init_cmds[0]),
        .flags = {
            .use_qspi_interface = 1,
        },
    };
    const esp_lcd_panel_dev_config_t panel_config = {
        .reset_gpio_num = LCD_PIN_RST,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = LCD_BIT_PER_PIXEL,
        .vendor_config = (void *)&vendor_config,
    };

    ESP_LOGI(TAG, "Install SH8601 panel driver");
    esp_lcd_panel_handle_t panel = NULL;
    ESP_ERROR_CHECK(esp_lcd_new_panel_sh8601(io_handle, &panel_config, &panel));
    ESP_ERROR_CHECK(esp_lcd_panel_reset(panel));
    ESP_ERROR_CHECK(esp_lcd_panel_init(panel));
    ESP_ERROR_CHECK(esp_lcd_panel_disp_on_off(panel, true));

    app_display_t display = {
        .panel = panel,
        .touch = touch_init(),
    };
    return display;
}

static void log_chip_details(void)
{
    esp_chip_info_t chip_info;
    uint32_t flash_size = 0;

    esp_chip_info(&chip_info);
    ESP_ERROR_CHECK(esp_flash_get_size(NULL, &flash_size));

    ESP_LOGI(TAG, "Project: Waveshare ESP32-C6 Touch AMOLED 1.8 display test");
    ESP_LOGI(TAG, "Target: %s", CONFIG_IDF_TARGET);
    ESP_LOGI(TAG, "Revision: v%d.%d", chip_info.revision / 100, chip_info.revision % 100);
    ESP_LOGI(TAG, "Flash size detected: %" PRIu32 " MB", flash_size / (1024 * 1024));
    ESP_LOGI(TAG, "Free heap: %" PRIu32 " bytes", esp_get_free_heap_size());
}

void app_main(void)
{
    log_chip_details();

    ESP_ERROR_CHECK(storage_init());
    init_device_id();
    ESP_ERROR_CHECK(buttons_init());
    app_display_t display = display_init();
    ESP_ERROR_CHECK(axp2101_init_power_button_irq());
    ESP_ERROR_CHECK(i2s_audio_init());
    ESP_ERROR_CHECK(es8311_codec_init());
    render_command_screen(display.panel);
    esp_err_t wifi_ret = wifi_init_sta();
    if (wifi_ret != ESP_OK) {
        ESP_LOGW(TAG, "Wi-Fi startup incomplete: %s", esp_err_to_name(wifi_ret));
    } else {
        esp_err_t register_ret = register_device_metadata();
        if (register_ret != ESP_OK) {
            ESP_LOGW(TAG, "Device metadata registration incomplete: %s", esp_err_to_name(register_ret));
        }
    }
    ESP_LOGI(TAG, "Microphone level visualization ready; press PWR for PMU shutdown or hold BOOT for command capture");
    ESP_ERROR_CHECK(axp2101_clear_power_irqs());

    uint32_t tick = 0;
    int64_t next_event_poll_us = 0;

    while (true) {
        if (boot_button_pressed()) {
            run_command_interaction(display.panel);
        }

        if (axp2101_power_button_short_pressed()) {
            enter_power_off(display.panel);
        }

        int64_t now_us = esp_timer_get_time();
        if (s_wifi_ready && now_us >= next_event_poll_us) {
            next_event_poll_us = now_us + DEVICE_EVENT_POLL_INTERVAL_US;
            (void)poll_device_events(display.panel, display.touch);
        }

        if (!s_visualizer_enabled) {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }

        size_t bytes_read = 0;
        esp_err_t ret = i2s_channel_read(s_i2s_rx, s_audio_samples, sizeof(s_audio_samples), &bytes_read, pdMS_TO_TICKS(500));
        if (ret == ESP_OK) {
            const size_t sample_count = bytes_read / sizeof(s_audio_samples[0]);
            const float level = audio_level_from_samples(s_audio_samples, sample_count);
            render_audio_level(display.panel, level);
            if ((tick % 20) == 0) {
                ESP_LOGI(TAG, "mic level=%.3f bytes=%u", level, (unsigned int)bytes_read);
            }
        } else {
            ESP_LOGW(TAG, "I2S read skipped: %s", esp_err_to_name(ret));
            vTaskDelay(pdMS_TO_TICKS(50));
        }

        if ((tick++ % 40) == 0) {
            power_status_t power = {0};
            ESP_LOGI(TAG, "alive uptime_ms=%" PRIu32, esp_log_timestamp());
            render_power_status(display.panel);
            if (axp2101_read_power_status(&power) == ESP_OK) {
                ESP_LOGI(TAG, "power batt_present=%d percent=%d batt_mv=%u vbus_present=%d vbus_good=%d vbus_mv=%u charging=%d discharging=%d charge_state=%u system_mv=%u",
                         power.battery_present, power.percent, power.battery_mv,
                         power.vbus_present, power.vbus_good, power.vbus_mv,
                         power.charging, power.discharging, power.charge_state, power.system_mv);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}
