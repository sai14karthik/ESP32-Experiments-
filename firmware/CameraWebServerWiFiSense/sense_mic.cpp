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
  // One-pole DC blocker (PDM often has DC offset).
  int32_t dc_x1 = 0;
  int32_t dc_y1 = 0;
  // Soft AGC for collar/wearable: adapt from *pre-gain* levels so room noise
  // does not peg gain at max. Idle settles ~3×; speech targets ~−22 dBFS.
  float agc_gain = 3.0f;
  const float agc_idle = 3.0f;
  const float agc_target = 0.08f;  // linear ≈ −22 dBFS after gain
  const float agc_min = 1.0f;
  const float agc_max = 10.0f;
  const float agc_attack = 0.25f;   // louder → pull gain down faster
  const float agc_release = 0.04f;  // quieter → raise gain slowly
  const float agc_idle_tau = 0.01f;
  // Pre-gain floor: below this, treat as silence (do not chase target).
  const float agc_gate = 0.0008f;  // ≈ −62 dBFS pre-gain

  for (;;) {
    int n = s_i2s.readBytes(reinterpret_cast<char *>(samples), sizeof(samples));
    if (n < 2) {
      vTaskDelay(pdMS_TO_TICKS(2));
      continue;
    }

    int count = n / 2;
    double sum_sq = 0.0;
    double pre_sum_sq = 0.0;
    int peak_raw = 0;
    for (int i = 0; i < count; i++) {
      int32_t x = samples[i];
      int32_t y = x - dc_x1 + ((dc_y1 * 995) / 1000);
      dc_x1 = x;
      dc_y1 = y;
      pre_sum_sq += (double)y * (double)y;
      float yf = (float)y * agc_gain;
      if (yf > 30000.0f) {
        yf = 30000.0f;
      } else if (yf < -30000.0f) {
        yf = -30000.0f;
      }
      samples[i] = (int16_t)yf;
      sum_sq += (double)yf * (double)yf;
      int a = abs((int)yf);
      if (a > peak_raw) {
        peak_raw = a;
      }
    }
    ring_push(samples, (size_t)count);

    const double full_scale = 32768.0;
    double rms_lin = sqrt(sum_sq / (double)count) / full_scale;
    double pre_rms = sqrt(pre_sum_sq / (double)count) / full_scale;
    double peak = (double)peak_raw / full_scale;
    if (rms_lin < 1e-9) {
      rms_lin = 1e-9;
    }
    if (pre_rms > agc_gate) {
      float desired = agc_target / (float)pre_rms;
      if (desired > agc_max) {
        desired = agc_max;
      } else if (desired < agc_min) {
        desired = agc_min;
      }
      float alpha = (desired < agc_gain) ? agc_attack : agc_release;
      agc_gain += alpha * (desired - agc_gain);
    } else {
      agc_gain += agc_idle_tau * (agc_idle - agc_gain);
    }
    float rms_db = (float)(20.0 * log10(rms_lin));
    s_rms_db = rms_db;
    s_peak = (float)peak;

    // ~5 Hz USB prints — less CPU contention vs camera/HTTP (was ~50 Hz).
    uint32_t now = millis();
    if (now - last_print_ms >= 200) {
      last_print_ms = now;
      sense_serial_lock();
      Serial.printf(
          "rms=%.1f dBFS: peak=%.3f,agc=%.2f,rms:%.1f,peak:%.3f\n",
          rms_db,
          peak,
          agc_gain,
          rms_db,
          peak);
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
