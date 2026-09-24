#include "sense_csi.h"
#include "sense_serial.h"

#include <Arduino.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <string.h>

#include "lwip/ip_addr.h"
#include "ping/ping_sock.h"

// Keep camera + mic happy: modest CSI rate on USB.
static const uint32_t kPingHz = 50;
static const int kCsiBufMax = 512;
static const int kQueueLen = 24;
static const uint32_t kMinPrintGapMs = 20;  // ~50 CSI lines/s max (needs 921600 baud)

// Lab Mini often answers ICMP when the AP gateway does not (client isolation).
static const char *kPingFallbackHost = "10.128.93.13";

struct CsiRecord {
  uint8_t mac[6];
  int8_t rssi;
  uint8_t rate;
  int8_t noise_floor;
  uint8_t channel;
  uint32_t timestamp;
  uint16_t sig_len;
  uint8_t rx_format;
  uint8_t first_word_inv;
  uint16_t len;
  uint16_t n;
  int8_t data[kCsiBufMax];
};

static uint8_t s_ap_bssid[6];
static QueueHandle_t s_queue = nullptr;
static esp_ping_handle_t s_ping = nullptr;
static volatile bool s_ok = false;
static int s_seq = 0;
static volatile uint32_t s_cb = 0;
static volatile uint32_t s_kept = 0;
static volatile uint32_t s_dropped = 0;

static void on_ping_ok(esp_ping_handle_t, void *) {}

static void csi_rx_cb(void *ctx, wifi_csi_info_t *info) {
  (void)ctx;
  s_cb++;
  if (!info || !info->buf || info->len == 0) {
    return;
  }

  // Accept all CSI frames. LabPSK reply MACs often differ from WiFi.BSSID().
  int n = info->len;
  if (n > kCsiBufMax) {
    n = kCsiBufMax;
  }

  CsiRecord rec = {};
  memcpy(rec.mac, info->mac, 6);
  memcpy(rec.data, info->buf, n);
  rec.n = (uint16_t)n;
  rec.len = info->len;
  rec.first_word_inv = info->first_word_invalid ? 1 : 0;
  rec.rssi = info->rx_ctrl.rssi;
  rec.rate = info->rx_ctrl.rate;
#if defined(CONFIG_IDF_TARGET_ESP32S3)
  rec.noise_floor = 0;
#else
  rec.noise_floor = info->rx_ctrl.noise_floor;
#endif
  rec.channel = info->rx_ctrl.channel;
  rec.timestamp = info->rx_ctrl.timestamp;
  rec.sig_len = info->rx_ctrl.sig_len;
  rec.rx_format = 0;

  if (s_queue && xQueueSend(s_queue, &rec, 0) == pdTRUE) {
    s_kept++;
  } else {
    s_dropped++;
  }
}

static void csi_print_task(void *) {
  CsiRecord rec;
  static char line[3072];
  bool header = false;
  uint32_t last_print = 0;
  uint32_t last_stats = 0;

  for (;;) {
    if (xQueueReceive(s_queue, &rec, pdMS_TO_TICKS(500)) != pdTRUE) {
      uint32_t now = millis();
      if (now - last_stats >= 5000) {
        last_stats = now;
        sense_serial_lock();
        Serial.printf(
          "# CSI stats cb=%lu kept=%lu drop=%lu\n",
          (unsigned long)s_cb,
          (unsigned long)s_kept,
          (unsigned long)s_dropped
        );
        sense_serial_unlock();
      }
      continue;
    }
    uint32_t now = millis();
    if (now - last_print < kMinPrintGapMs) {
      continue;  // throttle USB
    }
    last_print = now;

    sense_serial_lock();
    if (!header) {
      Serial.println(
        "type,seq,mac,rssi,rate,noise_floor,fft_gain,agc_gain,channel,"
        "local_timestamp,sig_len,rx_format,len,first_word,data"
      );
      header = true;
    }

    int p = snprintf(
      line,
      sizeof(line),
      "CSI_DATA,%d,%02x:%02x:%02x:%02x:%02x:%02x,%d,%u,%d,0,0,%u,%lu,%u,%u,%u,%u,\"[",
      s_seq++,
      rec.mac[0],
      rec.mac[1],
      rec.mac[2],
      rec.mac[3],
      rec.mac[4],
      rec.mac[5],
      (int)rec.rssi,
      rec.rate,
      (int)rec.noise_floor,
      rec.channel,
      (unsigned long)rec.timestamp,
      rec.sig_len,
      rec.rx_format,
      rec.len,
      rec.first_word_inv
    );
    if (p > 0 && p < (int)sizeof(line)) {
      for (int i = 0; i < rec.n && p < (int)sizeof(line) - 8; i++) {
        p += snprintf(line + p, sizeof(line) - p, i + 1 < rec.n ? "%d," : "%d", (int)rec.data[i]);
      }
      p += snprintf(line + p, sizeof(line) - p, "]\"\n");
      if (p > 0 && p < (int)sizeof(line)) {
        Serial.write(reinterpret_cast<const uint8_t *>(line), p);
      }
    }
    sense_serial_unlock();
  }
}

static void stop_ping(void) {
  if (!s_ping) {
    return;
  }
  esp_ping_stop(s_ping);
  esp_ping_delete_session(s_ping);
  s_ping = nullptr;
}

static bool start_ping_to(IPAddress dest, const char *label) {
  if (dest[0] == 0 && dest[1] == 0 && dest[2] == 0 && dest[3] == 0) {
    return false;
  }
  ip_addr_t target;
  IP_ADDR4(&target, dest[0], dest[1], dest[2], dest[3]);

  esp_ping_config_t cfg = ESP_PING_DEFAULT_CONFIG();
  cfg.target_addr = target;
  cfg.count = ESP_PING_COUNT_INFINITE;
  cfg.interval_ms = 1000 / kPingHz;
  cfg.data_size = 1;

  esp_ping_callbacks_t cbs = {};
  cbs.on_ping_success = on_ping_ok;
  if (esp_ping_new_session(&cfg, &cbs, &s_ping) != ESP_OK) {
    return false;
  }
  esp_ping_start(s_ping);
  Serial.printf("# CSI ping %s (%s) @ %lu/s\n", dest.toString().c_str(), label, (unsigned long)kPingHz);
  return true;
}

static void start_ping(void) {
  stop_ping();
  // Prefer Mini (known ICMP responder on LabPSK); else gateway.
  IPAddress mini;
  if (mini.fromString(kPingFallbackHost) && start_ping_to(mini, "mini")) {
    return;
  }
  if (start_ping_to(WiFi.gatewayIP(), "gw")) {
    return;
  }
  Serial.println("# CSI ping failed");
}

bool sense_csi_start(void) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("# CSI: WiFi not connected");
    return false;
  }

  if (!s_queue) {
    s_queue = xQueueCreate(kQueueLen, sizeof(CsiRecord));
    if (!s_queue) {
      Serial.println("# CSI: queue alloc failed");
      return false;
    }
    xTaskCreatePinnedToCore(csi_print_task, "sense_csi", 8192, nullptr, 1, nullptr, 0);
  }

  memcpy(s_ap_bssid, WiFi.BSSID(), 6);

  // ESP32-S3 classic CSI config + ACK dump (ping replies).
  wifi_csi_config_t csi = {};
  csi.lltf_en = true;
  csi.htltf_en = true;
  csi.stbc_htltf2_en = true;
  csi.ltf_merge_en = true;
  csi.channel_filter_en = false;
  csi.manu_scale = false;
  csi.shift = false;
  csi.dump_ack_en = true;

  wifi_promiscuous_filter_t filt = {};
  filt.filter_mask = WIFI_PROMIS_FILTER_MASK_ALL;
  esp_err_t err = esp_wifi_set_promiscuous_filter(&filt);
  if (err != ESP_OK) {
    Serial.printf("# CSI promiscuous filter 0x%x\n", (unsigned)err);
  }
  err = esp_wifi_set_promiscuous(true);
  if (err != ESP_OK) {
    Serial.printf("# CSI promiscuous 0x%x\n", (unsigned)err);
  }

  err = esp_wifi_set_csi_config(&csi);
  if (err != ESP_OK) {
    Serial.printf("# CSI config failed 0x%x\n", (unsigned)err);
    return false;
  }
  err = esp_wifi_set_csi_rx_cb(csi_rx_cb, s_ap_bssid);
  if (err != ESP_OK) {
    Serial.printf("# CSI cb failed 0x%x\n", (unsigned)err);
    return false;
  }
  err = esp_wifi_set_csi(true);
  if (err != ESP_OK) {
    Serial.printf("# CSI enable failed 0x%x\n", (unsigned)err);
    return false;
  }

  start_ping();
  s_ok = true;
  Serial.printf(
    "# CSI on %s BSSID %s ch %d (promiscuous+ack)\n",
    WiFi.SSID().c_str(),
    WiFi.BSSIDstr().c_str(),
    WiFi.channel()
  );
  return true;
}

bool sense_csi_ok(void) {
  return s_ok;
}
