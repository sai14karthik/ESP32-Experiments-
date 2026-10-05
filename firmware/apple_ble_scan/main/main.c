#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "host/ble_hs.h"
#include "host/ble_store.h"
#include "host/util/util.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "nvs_flash.h"

static const char *TAG = "apple_scan";

#define MAX_SEEN 24
#define PRINT_GAP_US (2LL * 1000 * 1000)
#define FRESH_US (10LL * 1000 * 1000)

typedef struct {
    bool used;
    uint8_t addr[6];
    uint8_t kind;
    int8_t rssi;
    int64_t last_print;
    int64_t last_seen;
} seen_t;

static seen_t s_seen[MAX_SEEN];
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static esp_timer_handle_t s_hb;

void ble_store_config_init(void);

static const char *kind_name(uint8_t kind)
{
    switch (kind) {
    case 0x02:
        return "iBeacon";
    case 0x05:
        return "AirDrop";
    case 0x06:
        return "HomeKit";
    case 0x07:
        return "AirPods";
    case 0x08:
        return "Siri";
    case 0x09:
    case 0x0A:
        return "AirPlay";
    case 0x0C:
        return "Handoff";
    case 0x0D:
        return "WiFiSettings";
    case 0x0E:
        return "Hotspot";
    case 0x0F:
        return "WiFiJoin";
    case 0x10:
        return "Nearby";
    case 0x12:
        return "FindMy";
    default:
        return "Apple";
    }
}

static uint8_t pick_kind(const uint8_t *msg, uint8_t len)
{
    uint8_t first = 0xFF;
    uint8_t i = 0;

    while ((uint16_t)i + 1 < len) {
        uint8_t type = msg[i];
        uint8_t n = msg[i + 1];
        if ((uint16_t)i + 2u + n > len) {
            break;
        }
        if (first == 0xFF) {
            first = type;
        }
        if (type == 0x10) {
            return 0x10;
        }
        i = (uint8_t)(i + 2u + n);
    }
    return first;
}

static seen_t *find_slot(const uint8_t addr[6])
{
    seen_t *free_slot = NULL;
    int64_t oldest = INT64_MAX;
    seen_t *oldest_slot = &s_seen[0];

    for (int i = 0; i < MAX_SEEN; i++) {
        if (s_seen[i].used && memcmp(s_seen[i].addr, addr, 6) == 0) {
            return &s_seen[i];
        }
        if (!s_seen[i].used && free_slot == NULL) {
            free_slot = &s_seen[i];
        }
        if (s_seen[i].used && s_seen[i].last_seen < oldest) {
            oldest = s_seen[i].last_seen;
            oldest_slot = &s_seen[i];
        }
    }
    if (free_slot != NULL) {
        return free_slot;
    }
    return oldest_slot;
}

static void note_apple(const uint8_t addr[6], int8_t rssi, uint8_t kind)
{
    int64_t now = esp_timer_get_time();
    bool print = false;

    taskENTER_CRITICAL(&s_mux);
    seen_t *slot = find_slot(addr);
    if (!slot->used || memcmp(slot->addr, addr, 6) != 0 || slot->kind != kind) {
        print = true;
    } else if (now - slot->last_print >= PRINT_GAP_US) {
        print = true;
    }
    slot->used = true;
    memcpy(slot->addr, addr, 6);
    slot->kind = kind;
    slot->rssi = rssi;
    slot->last_seen = now;
    if (print) {
        slot->last_print = now;
    }
    taskEXIT_CRITICAL(&s_mux);

    if (print) {
        ESP_LOGI(TAG, "APPLE rssi=%d addr=%02x:%02x:%02x:%02x:%02x:%02x kind=%s",
                 rssi,
                 addr[5], addr[4], addr[3], addr[2], addr[1], addr[0],
                 kind_name(kind));
    }
}

static int gap_event(struct ble_gap_event *event, void *arg)
{
    struct ble_hs_adv_fields fields;

    (void)arg;
    if (event->type != BLE_GAP_EVENT_DISC) {
        return 0;
    }
    if (ble_hs_adv_parse_fields(&fields, event->disc.data, event->disc.length_data) != 0) {
        return 0;
    }
    if (fields.mfg_data == NULL || fields.mfg_data_len < 2) {
        return 0;
    }
    if (fields.mfg_data[0] != 0x4C || fields.mfg_data[1] != 0x00) {
        return 0;
    }

    uint8_t kind = 0xFF;
    if (fields.mfg_data_len > 2) {
        kind = pick_kind(fields.mfg_data + 2, (uint8_t)(fields.mfg_data_len - 2));
    }
    note_apple(event->disc.addr.val, event->disc.rssi, kind);
    return 0;
}

static void start_scan(void)
{
    uint8_t own_addr_type;
    struct ble_gap_disc_params params = {0};

    if (ble_hs_id_infer_auto(0, &own_addr_type) != 0) {
        ESP_LOGE(TAG, "no BLE address");
        return;
    }

    params.passive = 0;
    params.filter_duplicates = 0;
    int rc = ble_gap_disc(own_addr_type, BLE_HS_FOREVER, &params, gap_event, NULL);
    if (rc != 0) {
        ESP_LOGE(TAG, "scan failed rc=%d", rc);
        return;
    }
    ESP_LOGI(TAG, "listening for Apple BLE (company 0x004C). Keep the iPhone Bluetooth on.");
}

static void on_reset(int reason)
{
    ESP_LOGE(TAG, "BLE host reset reason=%d", reason);
}

static void on_sync(void)
{
    if (ble_hs_util_ensure_addr(0) != 0) {
        ESP_LOGE(TAG, "BLE address setup failed");
        return;
    }
    start_scan();
}

static void host_task(void *param)
{
    (void)param;
    nimble_port_run();
    nimble_port_freertos_deinit();
}

static void heartbeat(void *arg)
{
    int apple = 0;
    int nearby = 0;
    int8_t best = -127;
    int64_t now = esp_timer_get_time();

    (void)arg;
    taskENTER_CRITICAL(&s_mux);
    for (int i = 0; i < MAX_SEEN; i++) {
        if (!s_seen[i].used || now - s_seen[i].last_seen > FRESH_US) {
            continue;
        }
        apple++;
        if (s_seen[i].kind == 0x10) {
            nearby++;
        }
        if (s_seen[i].rssi > best) {
            best = s_seen[i].rssi;
        }
    }
    taskEXIT_CRITICAL(&s_mux);

    if (apple == 0) {
        ESP_LOGI(TAG, "SCAN apple=0");
    } else {
        ESP_LOGI(TAG, "SCAN apple=%d nearby=%d best_rssi=%d", apple, nearby, best);
    }
}

void app_main(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ESP_ERROR_CHECK(nimble_port_init());
    ble_hs_cfg.reset_cb = on_reset;
    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.store_status_cb = ble_store_util_status_rr;
    ble_store_config_init();
    nimble_port_freertos_init(host_task);

    const esp_timer_create_args_t args = {
        .callback = heartbeat,
        .name = "apple_hb",
    };
    ESP_ERROR_CHECK(esp_timer_create(&args, &s_hb));
    ESP_ERROR_CHECK(esp_timer_start_periodic(s_hb, 5 * 1000 * 1000));
}
