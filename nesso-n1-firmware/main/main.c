#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2c.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_lcd_panel_io.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "hal/spi_types.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

static const char *TAG = "nesso_n1";
#define FIRMWARE_PROJECT "nesso-n1-firmware"
#define FIRMWARE_VERSION "0.0.1d"
#define FIRMWARE_DEVICE_TYPE "arduino-nesso-n1"

#define LCD_HOST SPI2_HOST
#define LCD_H_RES 135
#define LCD_V_RES 240
#define LCD_X_OFFSET 52
#define LCD_Y_OFFSET 40
#define LCD_PIN_MOSI GPIO_NUM_21
#define LCD_PIN_SCLK GPIO_NUM_20
#define LCD_PIN_CS GPIO_NUM_17
#define LCD_PIN_DC GPIO_NUM_16

#define I2C_PORT I2C_NUM_0
#define I2C_PIN_SDA GPIO_NUM_10
#define I2C_PIN_SCL GPIO_NUM_8
#define I2C_FREQ_HZ 100000
#define IOE_KEY_ADDR 0x43
#define IOE_UI_ADDR 0x44
#define IOE_REG_DIRECTION 0x03
#define IOE_REG_OUTPUT 0x05
#define IOE_REG_HIGH_Z 0x07
#define IOE_REG_PULL_ENABLE 0x0B
#define IOE_REG_PULL_SELECT 0x0D
#define IOE_REG_INPUT 0x0F
#define IOE_KEY1_BIT BIT0
#define IOE_KEY2_BIT BIT1
#define IOE_LORA_LNA_EN_BIT BIT5
#define IOE_LORA_ANT_SW_BIT BIT6
#define IOE_LORA_NRST_BIT BIT7
#define IOE_PWR_OFF_PULSE_BIT BIT0
#define IOE_LCD_RESET_BIT BIT1
#define IOE_GROVE_POWER_EN_BIT BIT2
#define IOE_USB_VIN_DETECT_BIT BIT5
#define IOE_LCD_BACKLIGHT_BIT BIT6
#define IOE_GREEN_LED_BIT BIT7

#define BQ27220_ADDR 0x55
#define BQ27220_CMD_VOLTAGE 0x08
#define BQ27220_CMD_STATE_OF_CHARGE 0x2c
#define AW32001_ADDR 0x49
#define AW32001_REG_POWER 0x01
#define AW32001_REG_CHARGE_TIMER 0x05
#define AW32001_REG_CHIP_ID 0x0a
#define AW32001_CHIP_ID 0x49
#define AW32001_BMS_FIX_VALUE 0xa2
#define AW32001_CHARGE_DISABLE_BIT BIT3

#define BEEP_PIN GPIO_NUM_11
#define IR_TX_PIN GPIO_NUM_9
#define IMU_INT_PIN GPIO_NUM_3
#define GROVE_IO_0 GPIO_NUM_5
#define GROVE_IO_1 GPIO_NUM_4

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT BIT1
#define WIFI_MAX_RETRIES 5
#define REGISTER_INTERVAL_US (30LL * 1000LL * 1000LL)
#define EVENT_POLL_INTERVAL_US (5LL * 1000LL * 1000LL)
#define BATTERY_READ_INTERVAL_US (10LL * 1000LL * 1000LL)
#define ALERT_DISPLAY_TIME_US (5LL * 1000LL * 1000LL)
#define LOW_BATTERY_BOOT_MIN_PCT 10
#define LOW_BATTERY_BOOT_MIN_MV 3500
#define LOW_BATTERY_RECOVERY_SLEEP_US (60LL * 1000LL * 1000LL)
#define HTTP_RESPONSE_MAX 1024
#define DISPLAY_TEXT_MAX 192
#define LCD_BLOCK_LINES 24

typedef struct {
    char data[HTTP_RESPONSE_MAX];
    int length;
} http_response_t;

static EventGroupHandle_t s_wifi_event_group;
static SemaphoreHandle_t s_color_done;
static esp_lcd_panel_io_handle_t s_lcd_io;
static int s_wifi_retry_count;
static bool s_wifi_ready;
static char s_device_id[48] = "arduino-nesso-n1-unknown";
static char s_ip_addr[16] = "0.0.0.0";
static char s_display_text[DISPLAY_TEXT_MAX] = "Display ready.";
static int s_battery_pct = -1;
static int s_battery_mv = -1;
static uint32_t s_key1_count;
static uint32_t s_key2_count;
static uint16_t s_line_buffer[LCD_H_RES];
static uint16_t s_block_buffer[LCD_H_RES * LCD_BLOCK_LINES];

static uint16_t rgb565(uint8_t r, uint8_t g, uint8_t b)
{
    uint16_t color = (uint16_t)(((r & 0xf8) << 8) | ((g & 0xfc) << 3) | (b >> 3));
    return (uint16_t)((color << 8) | (color >> 8));
}

static bool color_done_cb(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_io_event_data_t *edata, void *user_ctx)
{
    BaseType_t task_woken = pdFALSE;
    SemaphoreHandle_t sem = (SemaphoreHandle_t)user_ctx;
    xSemaphoreGiveFromISR(sem, &task_woken);
    return task_woken == pdTRUE;
}

static esp_err_t lcd_cmd(uint8_t cmd, const void *data, size_t len)
{
    return esp_lcd_panel_io_tx_param(s_lcd_io, cmd, data, len);
}

static esp_err_t lcd_cmd1(uint8_t cmd, uint8_t value)
{
    return lcd_cmd(cmd, &value, 1);
}

static esp_err_t lcd_set_window(int x0, int y0, int x1, int y1)
{
    x0 += LCD_X_OFFSET;
    x1 += LCD_X_OFFSET;
    y0 += LCD_Y_OFFSET;
    y1 += LCD_Y_OFFSET;
    uint8_t col[] = {
        (uint8_t)(x0 >> 8), (uint8_t)x0,
        (uint8_t)((x1 - 1) >> 8), (uint8_t)(x1 - 1),
    };
    uint8_t row[] = {
        (uint8_t)(y0 >> 8), (uint8_t)y0,
        (uint8_t)((y1 - 1) >> 8), (uint8_t)(y1 - 1),
    };
    ESP_RETURN_ON_ERROR(lcd_cmd(0x2a, col, sizeof(col)), TAG, "set column");
    ESP_RETURN_ON_ERROR(lcd_cmd(0x2b, row, sizeof(row)), TAG, "set row");
    return ESP_OK;
}

static esp_err_t i2c_write_reg(uint8_t addr, uint8_t reg, uint8_t value)
{
    uint8_t data[] = {reg, value};
    return i2c_master_write_to_device(I2C_PORT, addr, data, sizeof(data), pdMS_TO_TICKS(100));
}

static esp_err_t i2c_read_reg(uint8_t addr, uint8_t reg, uint8_t *value)
{
    return i2c_master_write_read_device(I2C_PORT, addr, &reg, 1, value, 1, pdMS_TO_TICKS(250));
}

static esp_err_t i2c_read_word(uint8_t addr, uint8_t reg, uint16_t *value)
{
    uint8_t data[2] = {0};
    esp_err_t ret = i2c_master_write_read_device(I2C_PORT, addr, &reg, 1, data, sizeof(data), pdMS_TO_TICKS(1000));
    if (ret != ESP_OK) {
        ret = i2c_master_write_to_device(I2C_PORT, addr, &reg, 1, pdMS_TO_TICKS(250));
        if (ret == ESP_OK) {
            vTaskDelay(pdMS_TO_TICKS(2));
            ret = i2c_master_read_from_device(I2C_PORT, addr, data, sizeof(data), pdMS_TO_TICKS(1000));
        }
    }
    if (ret == ESP_OK) {
        *value = (uint16_t)data[0] | ((uint16_t)data[1] << 8);
    }
    return ret;
}

static void i2c_init(void)
{
    const i2c_config_t config = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = I2C_PIN_SDA,
        .scl_io_num = I2C_PIN_SCL,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = I2C_FREQ_HZ,
    };
    ESP_ERROR_CHECK(i2c_param_config(I2C_PORT, &config));
    ESP_ERROR_CHECK(i2c_driver_install(I2C_PORT, config.mode, 0, 0, 0));
}

static void charger_init(void)
{
    uint8_t chip_id = 0;
    esp_err_t ret = i2c_read_reg(AW32001_ADDR, AW32001_REG_CHIP_ID, &chip_id);
    if (ret != ESP_OK || chip_id != AW32001_CHIP_ID) {
        ESP_LOGW(TAG, "AW32001 charger not detected: ret=%s chip=0x%02x", esp_err_to_name(ret), chip_id);
        return;
    }

    uint8_t charge_timer = 0;
    if (i2c_read_reg(AW32001_ADDR, AW32001_REG_CHARGE_TIMER, &charge_timer) == ESP_OK) {
        ESP_ERROR_CHECK_WITHOUT_ABORT(i2c_write_reg(AW32001_ADDR, AW32001_REG_CHARGE_TIMER, charge_timer & 0x1f));
    }

    ESP_ERROR_CHECK_WITHOUT_ABORT(i2c_write_reg(AW32001_ADDR, AW32001_REG_POWER, AW32001_BMS_FIX_VALUE));

    uint8_t power = 0;
    if (i2c_read_reg(AW32001_ADDR, AW32001_REG_POWER, &power) == ESP_OK) {
        ESP_ERROR_CHECK_WITHOUT_ABORT(i2c_write_reg(AW32001_ADDR, AW32001_REG_POWER,
                                                    power & (uint8_t)~AW32001_CHARGE_DISABLE_BIT));
    }
}

static void ioe_init(void)
{
    uint8_t ui_dir = IOE_PWR_OFF_PULSE_BIT | IOE_LCD_RESET_BIT | IOE_GROVE_POWER_EN_BIT |
                     IOE_LCD_BACKLIGHT_BIT | IOE_GREEN_LED_BIT;
    uint8_t ui_out = IOE_LCD_RESET_BIT | IOE_LCD_BACKLIGHT_BIT | IOE_GREEN_LED_BIT |
                     IOE_GROVE_POWER_EN_BIT;
    uint8_t lora_dir = IOE_LORA_LNA_EN_BIT | IOE_LORA_ANT_SW_BIT | IOE_LORA_NRST_BIT;
    uint8_t lora_safe_out = 0;

    ESP_ERROR_CHECK(i2c_write_reg(IOE_UI_ADDR, IOE_REG_OUTPUT, 0));
    ESP_ERROR_CHECK(i2c_write_reg(IOE_UI_ADDR, IOE_REG_DIRECTION, ui_dir));
    ESP_ERROR_CHECK(i2c_write_reg(IOE_UI_ADDR, IOE_REG_HIGH_Z, (uint8_t)~ui_dir));
    ESP_ERROR_CHECK(i2c_write_reg(IOE_UI_ADDR, IOE_REG_OUTPUT, ui_out & ~IOE_LCD_RESET_BIT));
    vTaskDelay(pdMS_TO_TICKS(30));
    ESP_ERROR_CHECK(i2c_write_reg(IOE_UI_ADDR, IOE_REG_OUTPUT, ui_out));

    /* Keep all LoRa front-end control lines inactive until antenna-safe support is added. */
    ESP_ERROR_CHECK(i2c_write_reg(IOE_KEY_ADDR, IOE_REG_OUTPUT, lora_safe_out));
    ESP_ERROR_CHECK(i2c_write_reg(IOE_KEY_ADDR, IOE_REG_DIRECTION, lora_dir));
    ESP_ERROR_CHECK(i2c_write_reg(IOE_KEY_ADDR, IOE_REG_HIGH_Z, (uint8_t)~lora_dir));
    ESP_ERROR_CHECK(i2c_write_reg(IOE_KEY_ADDR, IOE_REG_PULL_SELECT, IOE_KEY1_BIT | IOE_KEY2_BIT));
    ESP_ERROR_CHECK(i2c_write_reg(IOE_KEY_ADDR, IOE_REG_PULL_ENABLE, IOE_KEY1_BIT | IOE_KEY2_BIT));
}

static esp_err_t lcd_write_color(int x0, int y0, int x1, int y1, const uint16_t *color, size_t bytes)
{
    ESP_RETURN_ON_ERROR(lcd_set_window(x0, y0, x1, y1), TAG, "set draw window");
    xSemaphoreTake(s_color_done, 0);
    ESP_RETURN_ON_ERROR(esp_lcd_panel_io_tx_color(s_lcd_io, 0x2c, color, bytes), TAG, "write color");
    ESP_RETURN_ON_FALSE(xSemaphoreTake(s_color_done, pdMS_TO_TICKS(1000)) == pdTRUE, ESP_ERR_TIMEOUT, TAG, "wait color");
    return ESP_OK;
}

static void fill_rect(int x0, int y0, int x1, int y1, uint16_t color)
{
    x0 = x0 < 0 ? 0 : x0;
    y0 = y0 < 0 ? 0 : y0;
    x1 = x1 > LCD_H_RES ? LCD_H_RES : x1;
    y1 = y1 > LCD_V_RES ? LCD_V_RES : y1;
    if (x1 <= x0 || y1 <= y0) {
        return;
    }

    int width = x1 - x0;
    int pixels_per_block = width * LCD_BLOCK_LINES;
    for (int i = 0; i < pixels_per_block; ++i) {
        s_block_buffer[i] = color;
    }
    for (int y = y0; y < y1;) {
        int lines = y1 - y;
        if (lines > LCD_BLOCK_LINES) {
            lines = LCD_BLOCK_LINES;
        }
        ESP_ERROR_CHECK(lcd_write_color(x0, y, x1, y + lines, s_block_buffer, width * lines * sizeof(uint16_t)));
        y += lines;
    }
}

static void lcd_init(void)
{
    s_color_done = xSemaphoreCreateBinary();
    ESP_ERROR_CHECK(s_color_done ? ESP_OK : ESP_ERR_NO_MEM);

    gpio_config_t outputs = {
        .pin_bit_mask = (1ULL << BEEP_PIN) | (1ULL << IR_TX_PIN),
        .mode = GPIO_MODE_OUTPUT,
    };
    ESP_ERROR_CHECK(gpio_config(&outputs));
    gpio_set_level(BEEP_PIN, 0);
    gpio_set_level(IR_TX_PIN, 0);
    ioe_init();
    vTaskDelay(pdMS_TO_TICKS(120));

    const spi_bus_config_t buscfg = {
        .sclk_io_num = LCD_PIN_SCLK,
        .mosi_io_num = LCD_PIN_MOSI,
        .miso_io_num = -1,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = LCD_H_RES * LCD_BLOCK_LINES * sizeof(uint16_t),
    };
    ESP_ERROR_CHECK(spi_bus_initialize(LCD_HOST, &buscfg, SPI_DMA_CH_AUTO));

    const esp_lcd_panel_io_spi_config_t io_config = {
        .dc_gpio_num = LCD_PIN_DC,
        .cs_gpio_num = LCD_PIN_CS,
        .pclk_hz = 20 * 1000 * 1000,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
        .spi_mode = 0,
        .trans_queue_depth = 1,
        .on_color_trans_done = color_done_cb,
        .user_ctx = s_color_done,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)LCD_HOST, &io_config, &s_lcd_io));

    ESP_ERROR_CHECK(lcd_cmd(0x01, NULL, 0));
    vTaskDelay(pdMS_TO_TICKS(150));
    ESP_ERROR_CHECK(lcd_cmd(0x11, NULL, 0));
    vTaskDelay(pdMS_TO_TICKS(120));
    ESP_ERROR_CHECK(lcd_cmd1(0x3a, 0x55));
    ESP_ERROR_CHECK(lcd_cmd1(0x36, 0x00));
    ESP_ERROR_CHECK(lcd_cmd(0x21, NULL, 0));
    ESP_ERROR_CHECK(lcd_cmd(0x13, NULL, 0));
    ESP_ERROR_CHECK(lcd_cmd(0x29, NULL, 0));
    vTaskDelay(pdMS_TO_TICKS(20));
}

static const uint8_t *glyph(char c)
{
    static const uint8_t blank[5] = {0};
    static const uint8_t bang[5] = {0x00, 0x00, 0x5f, 0x00, 0x00};
    static const uint8_t dash[5] = {0x08, 0x08, 0x08, 0x08, 0x08};
    static const uint8_t dot[5] = {0x00, 0x60, 0x60, 0x00, 0x00};
    static const uint8_t colon[5] = {0x00, 0x36, 0x36, 0x00, 0x00};
    static const uint8_t nums[][5] = {
        {0x3e,0x51,0x49,0x45,0x3e}, {0x00,0x42,0x7f,0x40,0x00},
        {0x42,0x61,0x51,0x49,0x46}, {0x21,0x41,0x45,0x4b,0x31},
        {0x18,0x14,0x12,0x7f,0x10}, {0x27,0x45,0x45,0x45,0x39},
        {0x3c,0x4a,0x49,0x49,0x30}, {0x01,0x71,0x09,0x05,0x03},
        {0x36,0x49,0x49,0x49,0x36}, {0x06,0x49,0x49,0x29,0x1e},
    };
    static const uint8_t letters[][5] = {
        {0x7e,0x11,0x11,0x11,0x7e}, {0x7f,0x49,0x49,0x49,0x36},
        {0x3e,0x41,0x41,0x41,0x22}, {0x7f,0x41,0x41,0x22,0x1c},
        {0x7f,0x49,0x49,0x49,0x41}, {0x7f,0x09,0x09,0x09,0x01},
        {0x3e,0x41,0x49,0x49,0x7a}, {0x7f,0x08,0x08,0x08,0x7f},
        {0x00,0x41,0x7f,0x41,0x00}, {0x20,0x40,0x41,0x3f,0x01},
        {0x7f,0x08,0x14,0x22,0x41}, {0x7f,0x40,0x40,0x40,0x40},
        {0x7f,0x02,0x0c,0x02,0x7f}, {0x7f,0x04,0x08,0x10,0x7f},
        {0x3e,0x41,0x41,0x41,0x3e}, {0x7f,0x09,0x09,0x09,0x06},
        {0x3e,0x41,0x51,0x21,0x5e}, {0x7f,0x09,0x19,0x29,0x46},
        {0x46,0x49,0x49,0x49,0x31}, {0x01,0x01,0x7f,0x01,0x01},
        {0x3f,0x40,0x40,0x40,0x3f}, {0x1f,0x20,0x40,0x20,0x1f},
        {0x3f,0x40,0x38,0x40,0x3f}, {0x63,0x14,0x08,0x14,0x63},
        {0x07,0x08,0x70,0x08,0x07}, {0x61,0x51,0x49,0x45,0x43},
    };
    if (c >= 'a' && c <= 'z') c = (char)(c - 'a' + 'A');
    if (c >= 'A' && c <= 'Z') return letters[c - 'A'];
    if (c >= '0' && c <= '9') return nums[c - '0'];
    if (c == '!') return bang;
    if (c == '-') return dash;
    if (c == '.') return dot;
    if (c == ':') return colon;
    return blank;
}

static void draw_char(int x, int y, char c, uint16_t fg, uint16_t bg, int scale)
{
    const uint8_t *g = glyph(c);
    for (int py = 0; py < 7 * scale; ++py) {
        int gy = py / scale;
        for (int px = 0; px < 5 * scale; ++px) {
            int gx = px / scale;
            s_line_buffer[px] = (g[gx] & (1U << gy)) ? fg : bg;
        }
        ESP_ERROR_CHECK(lcd_write_color(x, y + py, x + 5 * scale, y + py + 1, s_line_buffer, 5 * scale * sizeof(uint16_t)));
    }
}

static int text_line_width(const char *text, int len, int scale)
{
    return len > 0 ? ((len - 1) * 6 + 5) * scale : 0;
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

static void draw_text_centered(int y, int max_width, int max_lines, const char *text, uint16_t fg, uint16_t bg, int scale)
{
    const int char_w = 6 * scale;
    const int max_chars = max_width / char_w;
    const char *cursor = text;

    for (int line = 0; line < max_lines && *cursor != '\0'; ++line) {
        while (*cursor == ' ') cursor++;
        int len = 0;
        int last_space = -1;
        while (cursor[len] != '\0' && cursor[len] != '\n' && len < max_chars) {
            if (cursor[len] == ' ') last_space = len;
            len++;
        }
        if (cursor[len] != '\0' && cursor[len] != '\n' && last_space > 0) {
            len = last_space;
        }
        int x = (LCD_H_RES - text_line_width(cursor, len, scale)) / 2;
        for (int i = 0; i < len; ++i) {
            draw_char(x + i * char_w, y, cursor[i], fg, bg, scale);
        }
        cursor += len;
        while (*cursor == ' ') cursor++;
        if (*cursor == '\n') cursor++;
        y += 9 * scale;
    }
}

static void draw_text_at(int x, int y, const char *text, uint16_t fg, uint16_t bg, int scale)
{
    const int char_w = 6 * scale;
    for (int i = 0; text[i] != '\0'; ++i) {
        draw_char(x + i * char_w, y, text[i], fg, bg, scale);
    }
}

static bool read_battery(void)
{
    uint16_t pct = 0;
    uint16_t mv = 0;
    esp_err_t pct_ret = i2c_read_word(BQ27220_ADDR, BQ27220_CMD_STATE_OF_CHARGE, &pct);
    esp_err_t mv_ret = i2c_read_word(BQ27220_ADDR, BQ27220_CMD_VOLTAGE, &mv);
    if (pct_ret == ESP_OK && pct <= 1000) {
        s_battery_pct = pct > 100 ? 100 : pct;
    }
    if (mv_ret == ESP_OK && mv > 2500 && mv < 5000) {
        s_battery_mv = mv;
    }
    return pct_ret == ESP_OK || mv_ret == ESP_OK;
}

static bool battery_too_low_for_boot(void)
{
    if (s_battery_pct >= 0 && s_battery_pct < LOW_BATTERY_BOOT_MIN_PCT) {
        return true;
    }
    if (s_battery_mv > 0 && s_battery_mv < LOW_BATTERY_BOOT_MIN_MV) {
        return true;
    }
    return false;
}

static void enter_low_battery_recovery(void)
{
    ESP_LOGW(TAG, "Battery low for normal boot: pct=%d mv=%d; sleeping to allow charging",
             s_battery_pct, s_battery_mv);
    esp_sleep_enable_timer_wakeup(LOW_BATTERY_RECOVERY_SLEEP_US);
    esp_deep_sleep_start();
}

static void update_home_text(void)
{
    if (s_wifi_ready) {
        snprintf(s_display_text, sizeof(s_display_text), "Online\n%s", s_ip_addr);
    } else {
        strlcpy(s_display_text, "Wi-Fi offline", sizeof(s_display_text));
    }
}

static void render_battery_icon(void)
{
    const uint16_t bg = rgb565(6, 10, 14);
    const uint16_t border = rgb565(86, 112, 112);
    const uint16_t fill_ok = rgb565(60, 185, 130);
    const uint16_t fill_low = rgb565(220, 70, 70);
    const uint16_t fill_unknown = rgb565(86, 112, 112);
    const int x = 72;
    const int y = 17;

    fill_rect(x - 2, y - 2, x + 34, y + 18, bg);
    fill_rect(x, y, x + 28, y + 2, border);
    fill_rect(x, y + 12, x + 28, y + 14, border);
    fill_rect(x, y, x + 2, y + 14, border);
    fill_rect(x + 26, y, x + 28, y + 14, border);
    fill_rect(x + 28, y + 4, x + 32, y + 10, border);

    if (s_battery_pct >= 0) {
        int fill_w = clamp_int((s_battery_pct * 22) / 100, 1, 22);
        fill_rect(x + 4, y + 4, x + 4 + fill_w, y + 10, s_battery_pct <= 20 ? fill_low : fill_ok);
    } else if (s_battery_mv > 0) {
        fill_rect(x + 4, y + 4, x + 16, y + 10, fill_unknown);
    }
}

static void render_home(void)
{
    const uint16_t bg = rgb565(6, 10, 14);
    const uint16_t fg = rgb565(220, 235, 230);
    const uint16_t muted = rgb565(96, 168, 210);
    fill_rect(0, 0, LCD_H_RES, LCD_V_RES, bg);
    draw_text_at(8, 20, "NESSO N1", muted, bg, 1);
    render_battery_icon();
    draw_text_centered(74, 120, 6, s_display_text, fg, bg, 1);
    draw_text_centered(204, 120, 2, s_device_id, muted, bg, 1);
}

static void render_alert(const char *text)
{
    const uint16_t bg = rgb565(6, 10, 14);
    const uint16_t fg = rgb565(255, 246, 232);
    const uint16_t red = rgb565(225, 52, 60);
    strlcpy(s_display_text, text && text[0] ? text : "Alert", sizeof(s_display_text));
    fill_rect(0, 0, LCD_H_RES, LCD_V_RES, bg);
    fill_rect(34, 24, 101, 91, red);
    draw_text_centered(42, 67, 1, "!", fg, red, 3);
    draw_text_centered(128, 120, 6, s_display_text, fg, bg, 1);
    vTaskDelay(pdMS_TO_TICKS(ALERT_DISPLAY_TIME_US / 1000));
    update_home_text();
    render_home();
}

static void init_device_id(void)
{
    uint8_t mac[6] = {0};
    ESP_ERROR_CHECK(esp_read_mac(mac, ESP_MAC_WIFI_STA));
    snprintf(s_device_id, sizeof(s_device_id), "arduino-nesso-n1-%02x%02x%02x", mac[3], mac[4], mac[5]);
    ESP_LOGI(TAG, "Device ID: %s", s_device_id);
}

static esp_err_t http_event_handler(esp_http_client_event_t *evt)
{
    http_response_t *response = (http_response_t *)evt->user_data;
    if (evt->event_id == HTTP_EVENT_ON_DATA && response != NULL && evt->data_len > 0) {
        int copy = evt->data_len;
        if (copy > HTTP_RESPONSE_MAX - response->length - 1) {
            copy = HTTP_RESPONSE_MAX - response->length - 1;
        }
        if (copy > 0) {
            memcpy(response->data + response->length, evt->data, copy);
            response->length += copy;
            response->data[response->length] = '\0';
        }
    }
    return ESP_OK;
}

static esp_err_t post_json(const char *path, const char *body, int *status_out)
{
    char url[240] = {0};
    snprintf(url, sizeof(url), "%s%s", CONFIG_NESSO_N1_SERVER_URL, path);

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
    esp_err_t ret = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);

    if (status_out) {
        *status_out = status;
    }
    return ret;
}

static void send_button_event(const char *button, const char *event, uint32_t count)
{
    if (!s_wifi_ready || strlen(CONFIG_NESSO_N1_SERVER_URL) == 0) {
        return;
    }

    char path[96] = {0};
    char body[192] = {0};
    int status = 0;
    snprintf(path, sizeof(path), "/devices/%s/button", s_device_id);
    snprintf(body, sizeof(body),
             "{"
             "\"event\":\"%s\","
             "\"button\":\"%s\","
             "\"gpio\":-1,"
             "\"click_count\":%" PRIu32
             "}",
             event,
             button,
             count);

    esp_err_t ret = post_json(path, body, &status);
    if (ret == ESP_OK && status >= 200 && status < 300) {
        ESP_LOGI(TAG, "Button event sent: %s %s count=%" PRIu32, button, event, count);
    } else {
        ESP_LOGW(TAG, "Button event failed: %s %s err=%s status=%d", button, event, esp_err_to_name(ret), status);
    }
}

static void poll_buttons(void)
{
    static bool key1_down;
    static bool key2_down;
    static int stable_key1;
    static int stable_key2;

    uint8_t input = 0xff;
    if (i2c_read_reg(IOE_KEY_ADDR, IOE_REG_INPUT, &input) != ESP_OK) {
        return;
    }

    bool key1_now = (input & IOE_KEY1_BIT) == 0;
    bool key2_now = (input & IOE_KEY2_BIT) == 0;

    if (key1_now == key1_down) {
        stable_key1 = 0;
    } else if (++stable_key1 >= 3) {
        key1_down = key1_now;
        stable_key1 = 0;
        if (key1_down) {
            send_button_event("KEY1", "press", ++s_key1_count);
        }
    }

    if (key2_now == key2_down) {
        stable_key2 = 0;
    } else if (++stable_key2 >= 3) {
        key2_down = key2_now;
        stable_key2 = 0;
        if (key2_down) {
            send_button_event("KEY2", "press", ++s_key2_count);
        }
    }
}

static void register_with_server(void)
{
    if (!s_wifi_ready || strlen(CONFIG_NESSO_N1_SERVER_URL) == 0) {
        return;
    }

    char path[96] = {0};
    char body[1200] = {0};
    int status = 0;
    snprintf(path, sizeof(path), "/devices/%s/register", s_device_id);
    snprintf(body, sizeof(body),
             "{"
             "\"type\":\"display\","
             "\"device_type\":\"" FIRMWARE_DEVICE_TYPE "\","
             "\"model\":\"Arduino Nesso N1\","
             "\"firmware\":{"
             "\"project\":\"" FIRMWARE_PROJECT "\","
             "\"version\":\"" FIRMWARE_VERSION "\","
             "\"device_type\":\"" FIRMWARE_DEVICE_TYPE "\","
             "\"target\":\"%s\""
             "},"
             "\"capabilities\":[\"display\",\"alert\",\"device-events\",\"touch\",\"button\",\"imu\",\"battery\",\"buzzer\",\"ir\",\"qwiic\",\"grove\",\"lora-disabled\"],"
             "\"status\":{"
             "\"ip\":\"%s\","
             "\"target\":\"%s\","
             "\"display\":\"ST7789P3 135x240\","
             "\"touch\":\"FT6336U\","
             "\"imu\":\"BMI270\","
             "\"battery_gauge\":\"BQ27220YZFR\","
             "\"charge_controller\":\"AW32001ECSR\","
             "\"lora\":\"SX1262 disabled\","
             "\"i2c\":\"GPIO10 SDA GPIO8 SCL\","
             "\"battery_pct\":%d,"
             "\"battery_mv\":%d,"
             "\"buzzer_gpio\":11,"
             "\"ir_gpio\":9"
             "}"
             "}",
             CONFIG_IDF_TARGET,
             s_ip_addr,
             CONFIG_IDF_TARGET,
             s_battery_pct,
             s_battery_mv);

    esp_err_t ret = post_json(path, body, &status);
    if (ret == ESP_OK && status >= 200 && status < 300) {
        ESP_LOGI(TAG, "Registered with command server");
    } else {
        ESP_LOGW(TAG, "Registration failed: err=%s status=%d", esp_err_to_name(ret), status);
    }
}

static bool extract_json_string(const char *json, const char *key, char *out, size_t out_size)
{
    char pattern[40] = {0};
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *pos = strstr(json, pattern);
    if (!pos) return false;
    pos = strchr(pos + strlen(pattern), ':');
    if (!pos) return false;
    pos++;
    while (*pos == ' ' || *pos == '\t') pos++;
    if (*pos != '"') return false;
    pos++;
    size_t i = 0;
    while (*pos && *pos != '"' && i + 1 < out_size) {
        out[i++] = *pos++;
    }
    out[i] = '\0';
    return i > 0;
}

static void poll_events(void)
{
    if (!s_wifi_ready || strlen(CONFIG_NESSO_N1_SERVER_URL) == 0) {
        return;
    }

    char url[240] = {0};
    http_response_t response = {0};
    snprintf(url, sizeof(url), "%s/devices/%s/events", CONFIG_NESSO_N1_SERVER_URL, s_device_id);

    esp_http_client_config_t config = {
        .url = url,
        .timeout_ms = 2500,
        .event_handler = http_event_handler,
        .user_data = &response,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) {
        return;
    }
    esp_err_t ret = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    if (ret != ESP_OK || status < 200 || status >= 300 || strstr(response.data, "\"type\"") == NULL) {
        return;
    }

    char type[24] = {0};
    char display_text[DISPLAY_TEXT_MAX] = {0};
    extract_json_string(response.data, "type", type, sizeof(type));
    extract_json_string(response.data, "display_text", display_text, sizeof(display_text));
    ESP_LOGI(TAG, "Event type=%s display=%s", type, display_text);
    if (strcmp(type, "alert") == 0) {
        render_alert(display_text);
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
    ESP_RETURN_ON_FALSE(strlen(CONFIG_NESSO_N1_WIFI_SSID) > 0, ESP_ERR_INVALID_STATE, TAG, "Wi-Fi SSID is empty");

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
    strlcpy((char *)wifi_config.sta.ssid, CONFIG_NESSO_N1_WIFI_SSID, sizeof(wifi_config.sta.ssid));
    strlcpy((char *)wifi_config.sta.password, CONFIG_NESSO_N1_WIFI_PASSWORD, sizeof(wifi_config.sta.password));
    wifi_config.sta.threshold.authmode = strlen(CONFIG_NESSO_N1_WIFI_PASSWORD) > 0 ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT, pdFALSE, pdFALSE, pdMS_TO_TICKS(12000));
    return s_wifi_ready ? ESP_OK : ESP_FAIL;
}

static void storage_init(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    } else {
        ESP_ERROR_CHECK(ret);
    }
}

void app_main(void)
{
    storage_init();
    init_device_id();
    i2c_init();
    charger_init();
    read_battery();
    if (battery_too_low_for_boot()) {
        enter_low_battery_recovery();
    }
    lcd_init();
    update_home_text();
    render_home();

    if (wifi_init_sta() == ESP_OK) {
        read_battery();
        update_home_text();
        render_home();
        register_with_server();
    } else {
        update_home_text();
        render_home();
    }

    int64_t next_register = esp_timer_get_time() + REGISTER_INTERVAL_US;
    int64_t next_poll = 0;
    int64_t next_battery = esp_timer_get_time() + BATTERY_READ_INTERVAL_US;
    while (true) {
        int64_t now = esp_timer_get_time();
        if (s_wifi_ready && now >= next_register) {
            next_register = now + REGISTER_INTERVAL_US;
            register_with_server();
        }
        if (s_wifi_ready && now >= next_poll) {
            next_poll = now + EVENT_POLL_INTERVAL_US;
            poll_events();
        }
        if (now >= next_battery) {
            next_battery = now + BATTERY_READ_INTERVAL_US;
            if (read_battery()) {
                update_home_text();
                render_home();
            }
        }
        poll_buttons();
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}
