#pragma once

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
