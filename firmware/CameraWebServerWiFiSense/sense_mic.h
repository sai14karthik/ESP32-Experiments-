#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Start PDM mic on XIAO ESP32-S3 Sense (pins 42/41). Returns false on init failure. */
bool sense_mic_start(void);

/** Latest levels: rms_db in dBFS (~-80..0), peak linear 0..1. */
void sense_mic_get(float *rms_db, float *peak);

/** True after a successful sense_mic_start(). */
bool sense_mic_ok(void);

#ifdef __cplusplus
}
#endif
