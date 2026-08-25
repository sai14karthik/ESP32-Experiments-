#include <Arduino.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include "ping/ping_sock.h"
#include "lwip/ip_addr.h"
#include "secrets.h"
#include "csi_record.h"

// Shared wire format (shared/csi-core) — the same header the collector
// parses with. The UDP sender must emit exactly kFrameLen bytes.
#include <csi/frame.hpp>
static_assert(csi::kFrameLen == 517,
              "collector and firmware disagree on the CSI wire format");

// ── Output mode (both default on — set to 0 in secrets.h to disable) ────
#ifndef CSI_OUTPUT_UDP
#define CSI_OUTPUT_UDP 1
#endif
#ifndef CSI_OUTPUT_SERIAL
#define CSI_OUTPUT_SERIAL 1
#endif

#include "udp_sender.h"

// ── XIAO ESP32-C6 — WiFi CSI capture (connected STA) ─────────────────────
// Connects to 2.4 GHz WiFi, then streams Channel State Information to serial.
//
// CSI is only produced when the radio RECEIVES a packet, so we continuously
// ping the gateway: every echo reply is an RX frame that fires the CSI
// callback. Each validated sample is printed as one "CSI_DATA" CSV line.
//
// IMPORTANT — this is an 802.11ax (WiFi-6 / HE) chip. The CSI API here is the
// HE variant (wifi_csi_config_t == wifi_csi_acquire_config_t, rx_ctrl ==
// esp_wifi_rxctrl_t). Classic ESP32 CSI examples (lltf_en/htltf_en, rx_ctrl
// .mcs/.cwb/.sig_mode ...) DO NOT COMPILE on the C6. Verified against the
// installed IDF 5.5.4 headers; the only valid config/metadata fields are the
// ones used below.
//
// LED_BUILTIN (active-LOW): slow blink = connecting, fast blink = connected.

// ── tunables ─────────────────────────────────────────────────────────────
static const uint32_t CONNECT_TIMEOUT_MS      = 20000;
static const uint32_t LED_BLINK_CONNECTING_MS = 1000;
static const uint32_t LED_BLINK_CONNECTED_MS  = 50;
static const uint32_t PING_INTERVAL_MS        = 50;   // ~20 Hz RX → ~20 CSI/s. Lower for a faster stream.
static const uint8_t  CSI_VAL_SCALE           = 2;    // I/Q fixed-point scale, range 0..3 on this chip. Raise if values clip at ±127.
static const bool     USE_EXTERNAL_ANTENNA    = false; // false = onboard ceramic, true = U.FL connector

#define CSI_QUEUE_LEN 24

static QueueHandle_t csiQueue = nullptr;
static CsiUdpSender  *udpSender = nullptr;
static uint8_t  apBssid[6];
static volatile uint32_t csiRx = 0, csiDropped = 0, csiInvalid = 0;
static esp_ping_handle_t pingHandle = nullptr;
static bool csiStarted = false;

// LED state
static unsigned long lastLedToggle = 0;
static bool ledState = false;

static void setLed(bool on) {
  digitalWrite(LED_BUILTIN, LOW);
  // ledState = on; digitalWrite(LED_BUILTIN, on ? LOW : HIGH);
}

// ── WiFi status helpers ──────────────────────────────────────────────────
static const char *wifiStatusStr(wl_status_t s) {
  switch (s) {
    case WL_NO_SSID_AVAIL:   return "NO_SSID_AVAIL (network not found — check name/2.4GHz)";
    case WL_CONNECTED:       return "CONNECTED";
    case WL_CONNECT_FAILED:  return "CONNECT_FAILED (wrong password?)";
    case WL_CONNECTION_LOST: return "CONNECTION_LOST";
    case WL_DISCONNECTED:    return "DISCONNECTED";
    default:                 return "OTHER";
  }
}

static void printConnectionInfo() {
  Serial.println();
  Serial.println("# --- WiFi connected ---");
  Serial.printf("# SSID    : %s\n", WiFi.SSID().c_str());
  Serial.printf("# BSSID   : %s\n", WiFi.BSSIDstr().c_str());
  Serial.printf("# Channel : %d\n", WiFi.channel());
  Serial.printf("# RSSI    : %d dBm\n", WiFi.RSSI());
  Serial.printf("# IP      : %s\n", WiFi.localIP().toString().c_str());
  Serial.printf("# Gateway : %s\n", WiFi.gatewayIP().toString().c_str());
}

static bool connectWiFi(uint32_t timeoutMs) {
  Serial.printf("\n# Connecting to \"%s\" ...\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - start > timeoutMs) {
      Serial.printf("\n# Timed out. Last status: %s\n", wifiStatusStr(WiFi.status()));
      return false;
    }
    if (millis() - lastLedToggle >= LED_BLINK_CONNECTING_MS) {
      lastLedToggle = millis(); setLed(!ledState); Serial.print('.');
    }
    delay(10);
  }
  setLed(true);
  printConnectionInfo();
  return true;
}

// ── CSI receive callback — runs in the WiFi task, keep it TINY ───────────
// The driver frees info->buf right after we return, so copy it here. We push
// a fixed-size record to a queue; a separate task does the slow serial print.
static void csiRxCallback(void *ctx, wifi_csi_info_t *info) {
  if (!info || !info->buf || info->len == 0) return;

  // Drop the C6's intermittent stale/garbage CSI. This gate is essential.
  if (!info->rx_ctrl.rx_channel_estimate_info_vld) { csiInvalid++; return; }

  // Keep only frames from our AP (BSSID passed as ctx).
  if (ctx && memcmp(info->mac, (const uint8_t *)ctx, 6) != 0) return;

  int off = info->first_word_invalid ? 4 : 0;   // first I/Q pair invalid on HW limitation
  int n = (int)info->len - off;
  if (n <= 0) return;
  if (n > CSI_BUF_MAX) n = CSI_BUF_MAX;

  CsiRecord rec;
  memcpy(rec.data, info->buf + off, n);
  rec.n   = n;
  rec.len = info->len;
  memcpy(rec.mac, info->mac, 6);
  rec.first_word_inv = info->first_word_invalid ? 1 : 0;
  rec.rx_seq = info->rx_seq;

  const wifi_pkt_rx_ctrl_t &c = info->rx_ctrl;
  rec.rssi        = c.rssi;
  rec.rate        = c.rate;
  rec.noise_floor = c.noise_floor;
  rec.channel     = c.channel;
  rec.second      = c.second;
  rec.bb_format   = c.cur_bb_format;
  rec.single_mpdu = c.cur_single_mpdu;
  rec.sig_len     = c.sig_len;
  rec.rx_state    = c.rx_state;
  rec.timestamp   = c.timestamp;

  csiRx++;
  if (xQueueSend(csiQueue, &rec, 0) != pdTRUE) csiDropped++;   // never block the WiFi task
}

// ── Consumer task — serial + conditional UDP/serial output ───────────────
static void csiConsumerTask(void *) {
  CsiRecord rec;
  unsigned long lastStats = 0;
  for (;;) {
    if (xQueueReceive(csiQueue, &rec, pdMS_TO_TICKS(1000)) == pdTRUE) {
#if CSI_OUTPUT_UDP
      if (udpSender) udpSender->send(rec);
#endif

#if CSI_OUTPUT_SERIAL
      // Serial.printf("CSI_DATA,%02x:%02x:%02x:%02x:%02x:%02x,%d,%u,%d,%u,%u,%u,%u,%u,%u,%lu,%u,%u,%u,[",
      //               rec.mac[0], rec.mac[1], rec.mac[2], rec.mac[3], rec.mac[4], rec.mac[5],
      //               (int)rec.rssi, rec.rate, (int)rec.noise_floor, rec.channel, rec.second,
      //               rec.bb_format, rec.single_mpdu, rec.sig_len, rec.rx_state,
      //               (unsigned long)rec.timestamp, rec.rx_seq, rec.first_word_inv, rec.len);
      // for (int i = 0; i < rec.n; i++) {
      //   Serial.print((int)rec.data[i]);
      //   if (i + 1 < rec.n) Serial.print(' ');
      // }
      // Serial.println(']');
#endif
    }
    if (millis() - lastStats >= 2000) {
      lastStats = millis();
      // Serial.printf("# rx=%lu dropped=%lu invalid=%lu rssi=%d\n",
      //               (unsigned long)csiRx, (unsigned long)csiDropped,
      //               (unsigned long)csiInvalid, WiFi.RSSI());
    }
  }
}

// ── Generate steady RX by pinging the gateway forever ────────────────────
static void startPing() {
  IPAddress gw = WiFi.gatewayIP();
  ip_addr_t target;
  IP_ADDR4(&target, gw[0], gw[1], gw[2], gw[3]);

  esp_ping_config_t cfg = ESP_PING_DEFAULT_CONFIG();
  cfg.target_addr = target;
  cfg.count       = ESP_PING_COUNT_INFINITE;   // 0 = run forever
  cfg.interval_ms = PING_INTERVAL_MS;
  cfg.data_size   = 1;

  esp_ping_callbacks_t cbs = {};               // CSI arrives via the WiFi cb, not these
  if (esp_ping_new_session(&cfg, &cbs, &pingHandle) == ESP_OK) {
    esp_ping_start(pingHandle);
    Serial.printf("# Pinging gateway %s every %lu ms to drive CSI\n",
                  gw.toString().c_str(), (unsigned long)PING_INTERVAL_MS);
  } else {
    Serial.println("# WARNING: failed to start ping session — CSI will only come from beacons");
  }
}

// ── Enable CSI, register callback, start consumer + ping (once) ──────────
static void startCsiCapture() {
  memcpy(apBssid, WiFi.BSSID(), 6);

  // Only fields that exist in wifi_csi_acquire_config_t on the C6 (MAC v2).
  wifi_csi_config_t csi = {};
  csi.enable             = 1;
  csi.acquire_csi_legacy = 1;   // L-LTF from 11g/legacy frames (beacons, ACKs)
  csi.acquire_csi_ht20   = 1;   // HT-LTF from HT20 — most single-STA 11n traffic
  csi.acquire_csi_ht40   = 1;   // HT-LTF from HT40
  csi.acquire_csi_su     = 1;   // HE-LTF from HE20 SU — if the router uses 11ax
  csi.val_scale_cfg      = CSI_VAL_SCALE;

  ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi));
  ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(csiRxCallback, apBssid));
  ESP_ERROR_CHECK(esp_wifi_set_csi(true));

  csiQueue = xQueueCreate(CSI_QUEUE_LEN, sizeof(CsiRecord));

#if CSI_OUTPUT_UDP
  udpSender = new CsiUdpSender();
  bool udpOk = udpSender->begin(COLLECTOR_IP, COLLECTOR_PORT);
  if (!udpOk) {
    Serial.println("# WARNING: CsiUdpSender failed to initialize — UDP output disabled");
    delete udpSender;
    udpSender = nullptr;
  }
#endif

#if CSI_OUTPUT_UDP
  Serial.println("# Output: serial + UDP");
#elif CSI_OUTPUT_SERIAL
  Serial.println("# Output: serial only");
#else
  Serial.println("# Output: UDP only");
#endif

  xTaskCreate(csiConsumerTask, "csiConsumer", 4096, nullptr, 1, nullptr);

#if CSI_OUTPUT_SERIAL
  Serial.println("# columns: CSI_DATA,src_mac,rssi,rate,noise_floor,channel,second,"
                 "bb_format,single_mpdu,sig_len,rx_state,timestamp_us,rx_seq,first_word_invalid,len,[imag real imag real ...]");
  Serial.println("# bb_format: 0=11b 1=11g 2=HT 3=VHT 4=HE-SU 5=HE-MU 6=HE-ERSU 7=HE-TB");
#endif

  startPing();
  csiStarted = true;
  Serial.println("# CSI capture started");
}

void setup() {
  Serial.begin(115200);
  Serial.setTxTimeoutMs(0);  // USB-CDC: don't block csiConsumerTask (and thus UDP) if the host can't keep up
  pinMode(LED_BUILTIN, OUTPUT);
  setLed(false);
  delay(1000);

  Serial.println();
  Serial.println("# === XIAO ESP32-C6 — WiFi CSI ===");
  Serial.printf("# SDK: %s  CPU: %lu MHz\n", ESP.getSdkVersion(), (unsigned long)getCpuFrequencyMhz());
  if (strlen(WIFI_SSID) == 0) Serial.println("# *** WIFI_SSID empty — edit include/secrets.h ***");

  // Pin the antenna ONCE before WiFi (toggling mid-capture jumps CSI phase).
  // GPIO3 (WIFI_ENABLE) powers the RF switch; GPIO14 (WIFI_ANT_CONFIG) selects port.
  pinMode(WIFI_ENABLE, OUTPUT);
  digitalWrite(WIFI_ENABLE, LOW);
  delay(100);
  pinMode(WIFI_ANT_CONFIG, OUTPUT);
  digitalWrite(WIFI_ANT_CONFIG, USE_EXTERNAL_ANTENNA ? HIGH : LOW);

  setCpuFrequencyMhz(160);   // max clock → best CSI throughput

  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);      // power-save off: steady RX + precise CSI timestamps

  if (connectWiFi(CONNECT_TIMEOUT_MS)) startCsiCapture();
}

void wifiIsConnected(long now) {
  if (now - lastLedToggle >= LED_BLINK_CONNECTED_MS) {
    lastLedToggle = now; setLed(!ledState);
  }
  if (!csiStarted) startCsiCapture();
}

void wifiIsNOTConnected(long now) {
  if (now - lastLedToggle >= LED_BLINK_CONNECTING_MS) { lastLedToggle = now; setLed(!ledState); }
    Serial.printf("\n# Link down (%s) — reconnecting...\n", wifiStatusStr(WiFi.status()));
    connectWiFi(CONNECT_TIMEOUT_MS);      // CSI + ping persist across reconnects
}

void loop() {
  unsigned long now = millis();

  if (WiFi.status() == WL_CONNECTED) {    
    wifiIsConnected(now);
  } else {
    wifiIsNOTConnected(now);
  }
  delay(5);
}