#include <Arduino.h>
#include "esp_camera.h"
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include "board_config.h"
#include "upload_url.h"

//const char *ssid = "SaiPhone";
//const char *password = "123456789";

const char *ssid = "SpectrumSetup-EB9C";
const char *password = "unitedvideo788";

static WiFiClientSecure tls;
static HTTPClient http;
static bool httpReady = false;

static void connectUpload() {
  http.end();
  tls.setInsecure();
  tls.setTimeout(8000);
  http.setTimeout(8000);
  http.setReuse(true);
  if (!http.begin(tls, UPLOAD_URL)) {
    Serial.println("http.begin failed");
    httpReady = false;
    return;
  }
  http.addHeader("Content-Type", "image/jpeg");
  http.addHeader("ngrok-skip-browser-warning", "1");
  httpReady = true;
}

static void uploadFrame() {
  if (WiFi.status() != WL_CONNECTED) {
    return;
  }
  if (!httpReady) {
    connectUpload();
    if (!httpReady) {
      delay(1000);
      return;
    }
  }

  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("capture failed");
    delay(50);
    return;
  }

  int code = http.POST(fb->buf, fb->len);
  esp_camera_fb_return(fb);

  if (code < 200 || code >= 300) {
    Serial.printf("upload HTTP %d\n", code);
    httpReady = false;
    delay(400);
  }
}

void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(false);
  delay(200);

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
  config.frame_size = FRAMESIZE_QVGA;
  config.pixel_format = PIXFORMAT_JPEG;
  config.grab_mode = CAMERA_GRAB_LATEST;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  config.jpeg_quality = 12;
  config.fb_count = 2;

  if (!psramFound()) {
    config.fb_location = CAMERA_FB_IN_DRAM;
    config.fb_count = 1;
    config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed 0x%x\n", err);
    return;
  }

  sensor_t *s = esp_camera_sensor_get();
  if (s && s->id.PID == OV3660_PID) {
    s->set_vflip(s, 1);
    s->set_brightness(s, 1);
    s->set_saturation(s, -2);
  }
  if (s) {
    s->set_framesize(s, FRAMESIZE_QVGA);
    s->set_quality(s, 12);
  }

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(ssid, password);
  Serial.printf("WiFi %s\n", ssid);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print(".");
    if (millis() - start > 40000) {
      Serial.println("\nWi-Fi failed. Need SaiPhone hotspot (2.4 GHz) and antenna.");
      return;
    }
  }
  Serial.println();
  Serial.print("IP ");
  Serial.println(WiFi.localIP());
  Serial.printf("Upload %s\n", UPLOAD_URL);
  connectUpload();
}

void loop() {
  uploadFrame();
  delay(120);
}
