#include "sense_serial.h"

static SemaphoreHandle_t s_serial_mu = nullptr;

static void ensure_serial_mu(void) {
  if (!s_serial_mu) {
    s_serial_mu = xSemaphoreCreateMutex();
  }
}

void sense_serial_lock(void) {
  ensure_serial_mu();
  if (s_serial_mu) {
    xSemaphoreTake(s_serial_mu, portMAX_DELAY);
  }
}

void sense_serial_unlock(void) {
  if (s_serial_mu) {
    xSemaphoreGive(s_serial_mu);
  }
}
