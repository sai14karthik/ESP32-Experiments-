#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Enable Wi‑Fi CSI + gateway ping after STA is connected.
 * Prints CSI_DATA lines on Serial (lab-compatible layout).
 */
bool sense_csi_start(void);

bool sense_csi_ok(void);

#ifdef __cplusplus
}
#endif
