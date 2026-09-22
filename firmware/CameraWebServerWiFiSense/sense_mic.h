#pragma once

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/** PDM sample rate (Hz) — matches Sense I2S and Mini ffmpeg `-ar`. */
#define SENSE_MIC_SAMPLE_RATE 16000

/** Start PDM mic on XIAO ESP32-S3 Sense (pins 42/41). Returns false on init failure. */
bool sense_mic_start(void);

/** Latest levels: rms_db in dBFS (~-80..0), peak linear 0..1. */
void sense_mic_get(float *rms_db, float *peak);

/** True after a successful sense_mic_start(). */
bool sense_mic_ok(void);

/**
 * Copy up to `n` int16 mono samples from the capture ring into `dst`.
 * Blocks up to `wait_ms` for at least one sample. Returns samples copied (0 on timeout).
 */
size_t sense_mic_read(int16_t *dst, size_t n, uint32_t wait_ms);

#ifdef __cplusplus
}
#endif
