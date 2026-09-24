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

// ── Output mode (serial on for viz; UDP off by default — set 1 in secrets.h) ─
#ifndef CSI_OUTPUT_UDP
#define CSI_OUTPUT_UDP 0
#endif
#ifndef CSI_OUTPUT_SERIAL
#define CSI_OUTPUT_SERIAL 1
#endif

#if CSI_OUTPUT_UDP
#include "udp_sender.h"
#endif

#include <WiFiUdp.h>

// ── XIAO ESP32-C6 — WiFi CSI capture (connected STA) ─────────────────────
// Connects to 2.4 GHz WiFi, then streams Channel State Information to serial.
//
// CSI is only produced when the radio RECEIVES a packet, so we continuously
// ping the gateway and poke it with UDP: ACKs / echo replies fire the CSI
// callback. Each sample is printed as one "CSI_DATA" CSV line.
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
static const uint32_t PING_INTERVAL_MS        = 20;   // 50 Hz ping cadence
static const uint32_t PING_TIMEOUT_MS         = 1000; // must be >> RTT or every ping "fails"
static const uint32_t STIM_INTERVAL_MS        = 10;   // UDP poke → AP ACKs → CSI
// Prefer Mini (known reachable on LabPSK). Override in secrets.h if needed.
#ifndef CSI_PING_HOST
#define CSI_PING_HOST "10.128.93.13"
#endif
static const uint8_t  CSI_VAL_SCALE           = 2;    // I/Q fixed-point scale, range 0..3 on this chip. Raise if values clip at ±127.
static const bool     USE_EXTERNAL_ANTENNA    = false; // false = onboard ceramic, true = U.FL connector

#define CSI_QUEUE_LEN 64
#define CSI_LINE_MAX  1600

static QueueHandle_t csiQueue = nullptr;
#if CSI_OUTPUT_UDP
static CsiUdpSender  *udpSender = nullptr;
#endif
static uint8_t  apBssid[6];
static volatile uint32_t csiRx = 0, csiDropped = 0, csiInvalid = 0, csiOtherMac = 0;
static volatile uint32_t csiSeq = 0;
static volatile uint32_t pingOk = 0, pingFail = 0;
static esp_ping_handle_t pingHandle = nullptr;
static bool csiStarted = false;
static WiFiUDP stimUdp;
static IPAddress stimGw;

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
  WiFi.setSleep(false);
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
  if (!info || !info->buf) return;
  if (info->len == 0) return;

  // Espressif csi_recv_router does not gate on rx_channel_estimate_info_vld;
  // on LabPSK that bit often stays 0 even when buf/len are usable.
  if (!info->rx_ctrl.rx_channel_estimate_info_vld) {
    uint32_t v = csiInvalid;
    csiInvalid = v + 1;
  }

  // Keep only frames from our AP (BSSID passed as ctx). Optional: pass
  // nullptr to accept any transmitter (needed on some LabPSK paths where
  // info->mac is not the BSSID).
  if (ctx && memcmp(info->mac, (const uint8_t *)ctx, 6) != 0) {
    uint32_t v = csiOtherMac;
    csiOtherMac = v + 1;
    return;
  }

  int off = info->first_word_invalid ? 4 : 0;   // first I/Q pair invalid on HW limitation
  int n = (int)info->len - off;
  if (n <= 0) return;
  if (n > CSI_BUF_MAX) n = CSI_BUF_MAX;

  CsiRecord rec;
  memcpy(rec.data, info->buf + off, n);
  rec.n   = n;
  rec.len = (uint16_t)n;  // bytes actually printed / queued (after first-word skip)
  memcpy(rec.mac, info->mac, 6);
  rec.first_word_inv = info->first_word_invalid ? 1 : 0;
  uint32_t seq = csiSeq + 1;
  csiSeq = seq;
  rec.rx_seq = (uint16_t)seq;

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

  uint32_t rx = csiRx + 1;
  csiRx = rx;
  if (xQueueSend(csiQueue, &rec, 0) != pdTRUE) {
    uint32_t d = csiDropped + 1;
    csiDropped = d;
  }
}

// ── Consumer task — serial + conditional UDP/serial output ───────────────
static void csiConsumerTask(void *) {
  CsiRecord rec;
  char line[CSI_LINE_MAX];
  unsigned long lastStats = 0;
  uint32_t rxSnap = 0;
  for (;;) {
    if (xQueueReceive(csiQueue, &rec, pdMS_TO_TICKS(50)) == pdTRUE) {
#if CSI_OUTPUT_UDP
      if (udpSender) udpSender->send(rec);
#endif

#if CSI_OUTPUT_SERIAL
      int o = snprintf(
          line, sizeof(line),
          "CSI_DATA,%02x:%02x:%02x:%02x:%02x:%02x,%d,%u,%d,%u,%u,%u,%u,%u,%u,%lu,%u,%u,%u,[",
          rec.mac[0], rec.mac[1], rec.mac[2], rec.mac[3], rec.mac[4], rec.mac[5],
          (int)rec.rssi, rec.rate, (int)rec.noise_floor, rec.channel, rec.second,
          rec.bb_format, rec.single_mpdu, rec.sig_len, rec.rx_state,
          (unsigned long)rec.timestamp, rec.rx_seq, rec.first_word_inv, rec.len);
      if (o < 0) continue;
      for (int i = 0; i < rec.n && o < (int)sizeof(line) - 12; i++) {
        int n = snprintf(line + o, sizeof(line) - (size_t)o, i ? " %d" : "%d", (int)rec.data[i]);
        if (n < 0) { o = -1; break; }
        o += n;
      }
      if (o < 0 || o >= (int)sizeof(line) - 3) {
        uint32_t d = csiDropped + 1;
        csiDropped = d;
        continue;
      }
      line[o++] = ']';
      line[o++] = '\n';
      size_t wrote = Serial.write((const uint8_t *)line, (size_t)o);
      if (wrote != (size_t)o) {
        Serial.write('\n');
        uint32_t d = csiDropped + 1;
        csiDropped = d;
      }
#endif
    }
    if (millis() - lastStats >= 1000) {
      lastStats = millis();
      uint32_t rx = csiRx;
      uint32_t dps = rx - rxSnap;
      rxSnap = rx;
      Serial.printf("# csi/s=%lu rx=%lu dropped=%lu invalid=%lu ping_ok=%lu ping_fail=%lu rssi=%d\n",
                    (unsigned long)dps, (unsigned long)rx, (unsigned long)csiDropped,
                    (unsigned long)csiInvalid, (unsigned long)pingOk, (unsigned long)pingFail,
                    WiFi.RSSI());
    }
  }
}

static void onPingSuccess(esp_ping_handle_t, void *) { pingOk++; }
static void onPingTimeout(esp_ping_handle_t, void *) { pingFail++; }

// ── Generate steady RX by pinging a reachable host forever ───────────────
static bool resolvePingTarget(IPAddress *out) {
  IPAddress host;
  if (host.fromString(CSI_PING_HOST)) {
    *out = host;
    return true;
  }
  *out = WiFi.gatewayIP();
  return (uint32_t)(*out) != 0;
}

static void startPing() {
  IPAddress dest;
  if (!resolvePingTarget(&dest)) {
    Serial.println("# WARNING: no ping target — CSI will only come from beacons/ACKs");
    return;
  }
  ip_addr_t target;
  IP_ADDR4(&target, dest[0], dest[1], dest[2], dest[3]);

  esp_ping_config_t cfg = ESP_PING_DEFAULT_CONFIG();
  cfg.target_addr     = target;
  cfg.count           = ESP_PING_COUNT_INFINITE;
  cfg.interval_ms     = PING_INTERVAL_MS;
  cfg.timeout_ms      = PING_TIMEOUT_MS;
  cfg.data_size       = 32;
  cfg.task_stack_size = 4096;

  esp_ping_callbacks_t cbs = {};
  cbs.on_ping_success = onPingSuccess;
  cbs.on_ping_timeout = onPingTimeout;
  if (esp_ping_new_session(&cfg, &cbs, &pingHandle) == ESP_OK) {
    esp_ping_start(pingHandle);
    Serial.printf("# Pinging %s every %lu ms (timeout %lu ms) to drive CSI\n",
                  dest.toString().c_str(),
                  (unsigned long)PING_INTERVAL_MS,
                  (unsigned long)PING_TIMEOUT_MS);
  } else {
    Serial.println("# WARNING: failed to start ping session — CSI will only come from beacons");
  }
}

// Extra TX → AP ACKs (dump_ack_en). Helps when ICMP replies are sparse.
static void stimTask(void *) {
  IPAddress dest;
  if (!resolvePingTarget(&dest)) dest = WiFi.gatewayIP();
  stimGw = dest;
  stimUdp.begin(0);
  const uint8_t payload[] = {'C', 'S', 'I'};
  for (;;) {
    if (WiFi.status() == WL_CONNECTED) {
      // Discard port — Mini/gw may ignore payload; AP still ACKs the 802.11 frame.
      if (stimUdp.beginPacket(stimGw, 9)) {
        stimUdp.write(payload, sizeof(payload));
        stimUdp.endPacket();
      }
    }
    vTaskDelay(pdMS_TO_TICKS(STIM_INTERVAL_MS));
  }
}

// ── Enable CSI, register callback, start consumer + ping (once) ──────────
static void startCsiCapture() {
  memcpy(apBssid, WiFi.BSSID(), 6);

  // Match esp-csi csi_recv_router C6 config (MAC v2 / CONFIG_SOC_WIFI_MAC_VERSION_NUM==2).
  wifi_csi_config_t csi = {};
  csi.enable                 = 1;
  csi.acquire_csi_legacy     = 1;   // L-LTF (beacons, many data/ACK)
  csi.acquire_csi_ht20       = 1;
  csi.acquire_csi_ht40       = 1;
  csi.acquire_csi_su         = 1;   // Lab AP may reply as HE-SU
  csi.acquire_csi_mu         = 0;
  csi.acquire_csi_dcm        = 0;
  csi.acquire_csi_beamformed = 0;
  csi.acquire_csi_he_stbc    = 2;
  csi.val_scale_cfg          = CSI_VAL_SCALE;  // 0..3 on C6
  csi.dump_ack_en            = 1;   // ping ACKs + echo replies both matter

  ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi));
  // nullptr = no BSSID filter. LabPSK sometimes reports a non-BSSID TA in
  // info->mac; filtering would yield zero CSI_DATA lines for the visualizer.
  ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(csiRxCallback, nullptr));
  ESP_ERROR_CHECK(esp_wifi_set_csi(true));
  // IDF: STA alone only sees AP-directed frames; promiscuous pulls far more CSI.
  ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
  esp_wifi_set_ps(WIFI_PS_NONE);
  Serial.printf("# CSI filter: any MAC + promiscuous (AP BSSID %02x:%02x:%02x:%02x:%02x:%02x)\n",
                apBssid[0], apBssid[1], apBssid[2], apBssid[3], apBssid[4], apBssid[5]);

  csiQueue = xQueueCreate(CSI_QUEUE_LEN, sizeof(CsiRecord));

#if CSI_OUTPUT_UDP
  udpSender = new CsiUdpSender();
  bool udpOk = udpSender->begin(COLLECTOR_IP, COLLECTOR_PORT);
  if (!udpOk) {
    Serial.println("# WARNING: CsiUdpSender failed to initialize — UDP output disabled");
    delete udpSender;
    udpSender = nullptr;
  }
  Serial.println("# Output: serial + UDP");
#elif CSI_OUTPUT_SERIAL
  Serial.println("# Output: serial only (UDP collector off)");
#else
  Serial.println("# Output: none");
#endif

  xTaskCreate(csiConsumerTask, "csiConsumer", 6144, nullptr, 2, nullptr);
  xTaskCreate(stimTask, "csiStim", 3072, nullptr, 1, nullptr);

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
  // Prefer complete lines over never-block; corrupt CSI breaks the visualizer.
  Serial.setTxTimeoutMs(50);
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