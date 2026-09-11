#include "sense_mic.h"
#include "sense_serial.h"

#include <Arduino.h>
#include <ESP_I2S.h>
#include <math.h>

// Seeed XIAO ESP32-S3 Sense PDM microphone
static const int MIC_CLK = 42;
static const int MIC_DATA = 41;

static I2SClass s_i2s;
static volatile float s_rms_db = -80.0f;
static volatile float s_peak = 0.0f;
static volatile bool s_ok = false;

static void sense_mic_task(void *arg) {
  (void)arg;
  int16_t samples[256];
  uint32_t last_print_ms = 0;

  for (;;) {
    int n = s_i2s.readBytes(reinterpret_cast<char *>(samples), sizeof(samples));
    if (n < 2) {
      vTaskDelay(pdMS_TO_TICKS(5));
      continue;
    }

    int count = n / 2;
    double sum_sq = 0.0;
    int peak_raw = 0;
    for (int i = 0; i < count; i++) {
      int v = samples[i];
      sum_sq += (double)v * (double)v;
      int a = abs(v);
      if (a > peak_raw) {
        peak_raw = a;
      }
    }

    const double full_scale = 32768.0;
    double rms_lin = sqrt(sum_sq / (double)count) / full_scale;
    double peak = (double)peak_raw / full_scale;
    if (rms_lin < 1e-9) {
      rms_lin = 1e-9;
    }
    float rms_db = (float)(20.0 * log10(rms_lin));
    s_rms_db = rms_db;
    s_peak = (float)peak;

    // ~50 Hz USB prints (mutex keeps CSI_DATA lines intact).
    uint32_t now = millis();
    if (now - last_print_ms >= 20) {
      last_print_ms = now;
      sense_serial_lock();
      Serial.printf("rms=%.1f dBFS: peak=%.3f,rms:%.1f,peak:%.3f\n", rms_db, peak, rms_db, peak);
      sense_serial_unlock();
    }
  }
}

bool sense_mic_start(void) {
  s_i2s.setPinsPdmRx(MIC_CLK, MIC_DATA);
  if (!s_i2s.begin(I2S_MODE_PDM_RX, 16000, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) {
    Serial.println("Mic init failed");
    s_ok = false;
    return false;
  }
  s_ok = true;
  Serial.println("Mic ready (PDM Sense)");
  xTaskCreatePinnedToCore(sense_mic_task, "sense_mic", 4096, nullptr, 1, nullptr, 0);
  return true;
}

void sense_mic_get(float *rms_db, float *peak) {
  if (rms_db) {
    *rms_db = s_rms_db;
  }
  if (peak) {
    *peak = s_peak;
  }
}

bool sense_mic_ok(void) {
  return s_ok;
}
