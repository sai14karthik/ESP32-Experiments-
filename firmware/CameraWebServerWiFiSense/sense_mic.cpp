#include "sense_mic.h"
#include "sense_serial.h"

#include <Arduino.h>
#include <ESP_I2S.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <math.h>
#include <string.h>

// Seeed XIAO ESP32-S3 Sense PDM microphone
static const int MIC_CLK = 42;
static const int MIC_DATA = 41;

// ~1 s of mono int16 @ 16 kHz
static const size_t RING_SAMPLES = SENSE_MIC_SAMPLE_RATE;

static I2SClass s_i2s;
static volatile float s_rms_db = -80.0f;
static volatile float s_peak = 0.0f;
static volatile bool s_ok = false;

static int16_t s_ring[RING_SAMPLES];
static size_t s_w = 0;
static size_t s_r = 0;
static size_t s_count = 0;
static SemaphoreHandle_t s_mu = nullptr;
static SemaphoreHandle_t s_data = nullptr;

static void ring_push(const int16_t *samples, size_t n) {
  if (!s_mu) {
    return;
  }
  xSemaphoreTake(s_mu, portMAX_DELAY);
  for (size_t i = 0; i < n; i++) {
    s_ring[s_w] = samples[i];
    s_w = (s_w + 1) % RING_SAMPLES;
    if (s_count < RING_SAMPLES) {
      s_count++;
    } else {
      // Overwrite oldest — advance read pointer.
      s_r = (s_r + 1) % RING_SAMPLES;
    }
  }
  xSemaphoreGive(s_mu);
  if (n > 0 && s_data) {
    xSemaphoreGive(s_data);
  }
}

static void sense_mic_task(void *arg) {
  (void)arg;
  int16_t samples[256];
  uint32_t last_print_ms = 0;
  // One-pole DC blocker + mild attenuate (PDM often has DC; sudden jumps → headphone "whoops").
  int32_t dc_x1 = 0;
  int32_t dc_y1 = 0;

  for (;;) {
    int n = s_i2s.readBytes(reinterpret_cast<char *>(samples), sizeof(samples));
    if (n < 2) {
      vTaskDelay(pdMS_TO_TICKS(2));
      continue;
    }

    int count = n / 2;
    double sum_sq = 0.0;
    int peak_raw = 0;
    for (int i = 0; i < count; i++) {
      int32_t x = samples[i];
      // y = x - x1 + R*y1, R≈0.995
      int32_t y = x - dc_x1 + ((dc_y1 * 995) / 1000);
      dc_x1 = x;
      dc_y1 = y;
      // ~0.7 gain + soft clip
      y = (y * 7) / 10;
      if (y > 30000) {
        y = 30000;
      } else if (y < -30000) {
        y = -30000;
      }
      samples[i] = (int16_t)y;
      sum_sq += (double)y * (double)y;
      int a = abs((int)y);
      if (a > peak_raw) {
        peak_raw = a;
      }
    }
    ring_push(samples, (size_t)count);

    const double full_scale = 32768.0;
    double rms_lin = sqrt(sum_sq / (double)count) / full_scale;
    double peak = (double)peak_raw / full_scale;
    if (rms_lin < 1e-9) {
      rms_lin = 1e-9;
    }
    float rms_db = (float)(20.0 * log10(rms_lin));
    s_rms_db = rms_db;
    s_peak = (float)peak;

    // ~5 Hz USB prints — less CPU contention vs camera/HTTP (was ~50 Hz).
    uint32_t now = millis();
    if (now - last_print_ms >= 200) {
      last_print_ms = now;
      sense_serial_lock();
      Serial.printf("rms=%.1f dBFS: peak=%.3f,rms:%.1f,peak:%.3f\n", rms_db, peak, rms_db, peak);
      sense_serial_unlock();
    }
  }
}

bool sense_mic_start(void) {
  if (!s_mu) {
    s_mu = xSemaphoreCreateMutex();
  }
  if (!s_data) {
    s_data = xSemaphoreCreateBinary();
  }
  if (!s_mu || !s_data) {
    Serial.println("Mic sync init failed");
    s_ok = false;
    return false;
  }

  s_i2s.setPinsPdmRx(MIC_CLK, MIC_DATA);
  if (!s_i2s.begin(I2S_MODE_PDM_RX, SENSE_MIC_SAMPLE_RATE, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) {
    Serial.println("Mic init failed");
    s_ok = false;
    return false;
  }
  s_ok = true;
  Serial.println("Mic ready (PDM Sense)");
  xTaskCreatePinnedToCore(sense_mic_task, "sense_mic", 4096, nullptr, 3, nullptr, 0);
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

size_t sense_mic_read(int16_t *dst, size_t n, uint32_t wait_ms) {
  if (!dst || n == 0 || !s_ok || !s_mu) {
    return 0;
  }

  size_t got = 0;
  TickType_t deadline = wait_ms == 0 ? 0 : (xTaskGetTickCount() + pdMS_TO_TICKS(wait_ms));

  while (got < n) {
    xSemaphoreTake(s_mu, portMAX_DELAY);
    while (got < n && s_count > 0) {
      dst[got++] = s_ring[s_r];
      s_r = (s_r + 1) % RING_SAMPLES;
      s_count--;
    }
    size_t remaining = s_count;
    xSemaphoreGive(s_mu);

    if (got >= n || wait_ms == 0) {
      break;
    }
    if (remaining > 0) {
      continue;
    }
    TickType_t now = xTaskGetTickCount();
    if (now >= deadline) {
      break;
    }
    TickType_t left = deadline - now;
    xSemaphoreTake(s_data, left);
  }
  return got;
}
