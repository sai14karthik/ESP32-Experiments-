#pragma once

#include <stdint.h>

#include "esp_err.h"

/**
 * Join the lab Wi-Fi from lab_wifi.local.h (generated, not committed).
 * Call after esp_wifi_start. On success the radio is on the AP channel;
 * ESP-NOW peers should use channel 0 so they follow it.
 */
esp_err_t lab_sta_connect(void);

/** Drop a channel-11 peer and add it again on the current lab channel. */
void lab_espnow_use_current_channel(const uint8_t mac[6]);

typedef void (*lab_report_fill_fn)(char *buf, size_t buflen, void *ctx);

/** Master only: push one status line per tick to the Mini. */
void lab_report_start(lab_report_fill_fn fill, void *ctx);

/** Copy one CSI frame. Register as the esp-radar filtered callback. */
void lab_csi_filtered_cb(void *ctx, const void *info);
/** node_id 0 = master, 1 = RX2, 2 = RX3. Pushes CSI lines to the Mini. */
void lab_csi_start(uint8_t node_id);

/** True after the lab STA has an IP. */
bool lab_wifi_ready(void);
