#include <Arduino.h>
#include <WiFi.h>
#include "esp_camera.h"
#include "board_config.h"

#include "OV2640.h"
#include "OV2640Streamer.h"
#include "CStreamer.h"

// Lab Wi‑Fi
const char *ssid = "LabHealthSecurePSK";
const char *password = "ZLMKAQm@UV2e9g8r7GW!";

// Tuned from esp32cam-rtsp (rzeldent) XIAO Sense board defaults + RTSP lab:
//   boards/esp32cam_seeed_xiao_esp32s3_sense.json — pins / 20 MHz / fb_count=2 / PSRAM
//   include/settings.h — JPEG ~12 default; we use 10 (clear + LabPSK-friendly)
// MediaMTX pulls TCP (mediamtx-repo docs/2-features/28-decrease-packet-loss.md).
static const uint16_t kRtspPort = 554;
static const uint32_t kMsecPerFrame = 100;  // 10 fps — steady Micro-RTSP over LabPSK TCP

OV2640 cam;
WiFiServer rtspServer(kRtspPort);
CStreamer *streamer = nullptr;

static camera_config_t xiao_cam_config() {
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.frame_size = FRAMESIZE_VGA;  // 640x480 — same class as esp32cam-rtsp max-stable
  config.pixel_format = PIXFORMAT_JPEG;
  config.grab_mode = CAMERA_GRAB_LATEST;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.jpeg_quality = 10;
  config.fb_count = 2;
  if (!psramFound()) {
    config.fb_location = CAMERA_FB_IN_DRAM;
    config.fb_count = 1;
    config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
    config.frame_size = FRAMESIZE_QVGA;
    config.jpeg_quality = 14;
  }
  return config;
}

static void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }
  Serial.println("WiFi lost — reconnecting");
  WiFi.disconnect();
  WiFi.begin(ssid, password);
  WiFi.setSleep(false);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("WiFi OK ");
    Serial.println(WiFi.localIP());
    if (streamer) {
      streamer->setURI(String(WiFi.localIP().toString()) + ":" + String(kRtspPort));
    }
  }
}

void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(false);
  delay(200);
  Serial.println();
  Serial.println("CameraRTSPWiFi (XIAO Sense → Micro-RTSP → MediaMTX TCP pull)");
  Serial.println("refs: esp32cam-rtsp XIAO board + mediamtx-repo RTSP proxy");

  esp_err_t err = cam.init(xiao_cam_config());
  if (err != ESP_OK) {
    Serial.printf("Camera init failed 0x%x\n", (unsigned)err);
    return;
  }

  sensor_t *s = esp_camera_sensor_get();
  if (s) {
    // XIAO Sense module orientation (same as CameraWebServerWiFi lab)
    s->set_vflip(s, 1);
    s->set_hmirror(s, 1);
    s->set_brightness(s, 0);
    s->set_saturation(s, 0);
    s->set_whitebal(s, 1);
    s->set_gain_ctrl(s, 1);
    s->set_exposure_ctrl(s, 1);
    s->set_framesize(s, FRAMESIZE_VGA);
    s->set_quality(s, 10);
  }

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  WiFi.begin(ssid, password);
  Serial.print("WiFi connecting");
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("WiFi OK ");
  Serial.println(WiFi.localIP());

  streamer = new OV2640Streamer(&cam);
  streamer->setURI(String(WiFi.localIP().toString()) + ":" + String(kRtspPort));

  rtspServer.begin();
  Serial.printf("RTSP: rtsp://%s:%u/mjpeg/1\n", WiFi.localIP().toString().c_str(), kRtspPort);
  Serial.println("Mini: XIAO_RTSP_URL=that ./scripts/mediamtx_run.sh");
  Serial.println("Watch: ./scripts/watch_xiao_rtsp.sh");
}

void loop() {
  if (!streamer) {
    delay(500);
    return;
  }

  static uint32_t lastWifiCheck = 0;
  if (millis() - lastWifiCheck > 5000) {
    lastWifiCheck = millis();
    ensureWifi();
  }

  streamer->handleRequests(0);

  static uint32_t lastimage = millis();
  uint32_t now = millis();
  if (streamer->anySessions()) {
    if (now - lastimage >= kMsecPerFrame || now < lastimage) {
      streamer->streamImage(now);
      lastimage = now;
    }
  }

  WiFiClient accepted = rtspServer.accept();
  if (accepted) {
    accepted.setNoDelay(true);  // TCP interleaved RTP — lower latency
    Serial.print("RTSP client: ");
    Serial.println(accepted.remoteIP());
    WiFiClient *rtspClient = new WiFiClient(accepted);
    streamer->addSession(rtspClient);
  }
}
