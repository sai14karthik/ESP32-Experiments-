#include <WiFi.h>
#include <esp_wifi.h>
#include "ping/ping_sock.h"
#include "lwip/ip_addr.h"

// Arduino port of Espressif get-started/csi_recv_router (method 1: ping AP, CSI from replies).
// Board: ESP32C5 Dev Module, Flash 4MB, USB CDC On Boot Enabled.
// Serial Monitor: 921600. Arduino cannot use esp_csi_gain_ctrl, so fft/agc print as 0.

//const char *ssid = "SpectrumSetup-EB9C";
//const char *password = "unitedvideo788";

const char *ssid = "LabHealthSecurePSK";
const char *password = "ZLMKAQm@UV2e9g8r7GW!";

static const uint32_t kPingHz = 100;
static const uint32_t kConnectTimeoutMs = 20000;
static const uint32_t kReconnectMs = 3000;
static const int kCsiBufMax = 512;
static const int kQueueLen = 24;

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

static uint8_t apBssid[6];
static QueueHandle_t csiQueue = nullptr;
static esp_ping_handle_t pingHandle = nullptr;
static bool csiStarted = false;
static int sCount = 0;
static unsigned long lastReconnectMs = 0;
static volatile uint32_t csiKept = 0;
static volatile uint32_t csiDropped = 0;
static volatile uint32_t csiOther = 0;
static volatile uint32_t pingOk = 0;

static bool macIsZero(const uint8_t *m) {
  return !m[0] && !m[1] && !m[2] && !m[3] && !m[4] && !m[5];
}

static void onPingOk(esp_ping_handle_t, void *) {
  pingOk++;
}

// Same rule as official wifi_csi_rx_cb: keep frames whose source MAC is the AP.
// Arduino C5 often reports 00:00:00:00:00:00; those are kept and labeled as the AP.
static void csiRxCallback(void *ctx, wifi_csi_info_t *info) {
  if (!info || !info->buf) {
    return;
  }
  if (!info->rx_ctrl.rx_channel_estimate_info_vld || info->len == 0) {
    return;
  }

  const uint8_t *ap = static_cast<const uint8_t *>(ctx);
  if (memcmp(info->mac, ap, 6) != 0 && !macIsZero(info->mac)) {
    csiOther++;
    return;
  }

  int n = info->len;
  if (n > kCsiBufMax) {
    n = kCsiBufMax;
  }

  CsiRecord rec = {};
  if (macIsZero(info->mac)) {
    memcpy(rec.mac, ap, 6);
  } else {
    memcpy(rec.mac, info->mac, 6);
  }
  memcpy(rec.data, info->buf, n);
  rec.n = n;
  rec.len = info->len;
  rec.first_word_inv = info->first_word_invalid ? 1 : 0;
  rec.rssi = info->rx_ctrl.rssi;
  rec.rate = info->rx_ctrl.rate;
  rec.noise_floor = info->rx_ctrl.noise_floor;
  rec.channel = info->rx_ctrl.channel;
  rec.timestamp = info->rx_ctrl.timestamp;
  rec.sig_len = info->rx_ctrl.sig_len;
  rec.rx_format = info->rx_ctrl.cur_bb_format;

  if (csiQueue && xQueueSend(csiQueue, &rec, 0) == pdTRUE) {
    csiKept++;
  } else {
    csiDropped++;
  }
}

static void csiPrintTask(void *) {
  CsiRecord rec;
  static char line[3072];
  bool header = false;
  for (;;) {
    if (xQueueReceive(csiQueue, &rec, pdMS_TO_TICKS(1000)) != pdTRUE) {
      continue;
    }
    if (!header) {
      Serial.println("type,seq,mac,rssi,rate,noise_floor,fft_gain,agc_gain,channel,local_timestamp,sig_len,rx_format,len,first_word,data");
      header = true;
    }

    int p = snprintf(line, sizeof(line),
                     "CSI_DATA,%d,%02x:%02x:%02x:%02x:%02x:%02x,%d,%u,%d,0,0,%u,%lu,%u,%u,%u,%u,\"[",
                     sCount++,
                     rec.mac[0], rec.mac[1], rec.mac[2], rec.mac[3], rec.mac[4], rec.mac[5],
                     (int)rec.rssi, rec.rate, (int)rec.noise_floor, rec.channel,
                     (unsigned long)rec.timestamp, rec.sig_len, rec.rx_format,
                     rec.len, rec.first_word_inv);
    if (p < 0 || p >= (int)sizeof(line)) {
      continue;
    }
    for (int i = 0; i < rec.n && p < (int)sizeof(line) - 8; i++) {
      p += snprintf(line + p, sizeof(line) - p, i + 1 < rec.n ? "%d," : "%d", (int)rec.data[i]);
    }
    p += snprintf(line + p, sizeof(line) - p, "]\"\n");
    if (p > 0 && p < (int)sizeof(line)) {
      Serial.write(reinterpret_cast<const uint8_t *>(line), p);
    }
  }
}

static void stopPing() {
  if (!pingHandle) {
    return;
  }
  esp_ping_stop(pingHandle);
  esp_ping_delete_session(pingHandle);
  pingHandle = nullptr;
}

static void wifiPingRouterStart() {
  stopPing();
  IPAddress gw = WiFi.gatewayIP();
  ip_addr_t target;
  IP_ADDR4(&target, gw[0], gw[1], gw[2], gw[3]);

  esp_ping_config_t cfg = ESP_PING_DEFAULT_CONFIG();
  cfg.target_addr = target;
  cfg.count = ESP_PING_COUNT_INFINITE;
  cfg.interval_ms = 1000 / kPingHz;
  cfg.data_size = 1;

  esp_ping_callbacks_t cbs = {};
  cbs.on_ping_success = onPingOk;
  if (esp_ping_new_session(&cfg, &cbs, &pingHandle) == ESP_OK) {
    esp_ping_start(pingHandle);
    Serial.printf("# ping %s %lu/s\n", gw.toString().c_str(), (unsigned long)kPingHz);
  } else {
    Serial.println("# ping failed");
  }
}

// Official C5 csi_config from csi_recv_router. Promiscuous is Arduino-C5 only:
// without it the CSI callback never runs in Arduino.
static void wifiCsiInit() {
  memcpy(apBssid, WiFi.BSSID(), 6);

  wifi_csi_config_t csi = {};
  csi.enable = true;
  csi.acquire_csi_legacy = true;
  csi.acquire_csi_force_lltf = 0;
  csi.acquire_csi_ht20 = true;
  csi.acquire_csi_ht40 = true;
  csi.acquire_csi_vht = false;
  csi.acquire_csi_su = false;
  csi.acquire_csi_mu = false;
  csi.acquire_csi_dcm = false;
  csi.acquire_csi_beamformed = false;
  csi.acquire_csi_he_stbc_mode = 2;
  csi.val_scale_cfg = 0;
  csi.dump_ack_en = false;

  wifi_promiscuous_filter_t filt = {};
  filt.filter_mask = WIFI_PROMIS_FILTER_MASK_ALL;
  ESP_ERROR_CHECK(esp_wifi_set_promiscuous_filter(&filt));
  ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
  ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi));
  ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(csiRxCallback, apBssid));
  ESP_ERROR_CHECK(esp_wifi_set_csi(true));

  csiStarted = true;
  Serial.printf("# CSI %s %s ch %d\n",
                WiFi.SSID().c_str(), WiFi.BSSIDstr().c_str(), WiFi.channel());
}

static bool connectWifi(uint32_t timeoutMs) {
  Serial.printf("# connecting to %s\n", ssid);
  WiFi.disconnect(true, false);
  delay(50);
  WiFi.begin(ssid, password);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - start > timeoutMs) {
      Serial.println("# wifi timeout");
      return false;
    }
    delay(100);
  }
  Serial.printf("# ip %s gw %s rssi %d\n",
                WiFi.localIP().toString().c_str(),
                WiFi.gatewayIP().toString().c_str(),
                WiFi.RSSI());
  return true;
}

void setup() {
  Serial.begin(921600);
  Serial.setDebugOutput(false);
  Serial.setTxTimeoutMs(0);
  delay(800);

  csiQueue = xQueueCreate(kQueueLen, sizeof(CsiRecord));
  xTaskCreate(csiPrintTask, "csiPrint", 8192, nullptr, 1, nullptr);

  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);

  if (connectWifi(kConnectTimeoutMs)) {
    wifiCsiInit();
    wifiPingRouterStart();
  }
}

void loop() {
  if (WiFi.status() == WL_CONNECTED) {
    if (!csiStarted) {
      wifiCsiInit();
      wifiPingRouterStart();
    }
    static unsigned long lastStats = 0;
    if (millis() - lastStats >= 5000) {
      lastStats = millis();
      Serial.printf("# kept=%lu dropped=%lu other=%lu ping=%lu rssi=%d\n",
                    (unsigned long)csiKept, (unsigned long)csiDropped,
                    (unsigned long)csiOther, (unsigned long)pingOk, WiFi.RSSI());
    }
    delay(50);
    return;
  }

  csiStarted = false;
  stopPing();
  const unsigned long now = millis();
  if (now - lastReconnectMs >= kReconnectMs) {
    lastReconnectMs = now;
    if (connectWifi(kConnectTimeoutMs)) {
      wifiCsiInit();
      wifiPingRouterStart();
    }
  }
}
