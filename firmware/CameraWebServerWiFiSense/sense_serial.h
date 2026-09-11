#pragma once

#include <Arduino.h>

/** Serialize USB prints so CSI_DATA lines are not interleaved with rms:. */
void sense_serial_lock(void);
void sense_serial_unlock(void);
