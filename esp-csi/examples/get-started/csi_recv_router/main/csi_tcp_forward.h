/*
 * TCP client that forwards CSI_DATA lines to an ingest host (Mac Mini).
 * USB serial remains the source of truth for local debugging.
 */
#pragma once

#include <stddef.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Start reconnect/send task. No-op if CONFIG_CSI_TCP_ENABLE is unset. */
void csi_tcp_forward_start(void);

/**
 * Copy a complete newline-terminated CSI line into the send queue.
 * Safe to call from the Wi-Fi CSI callback (non-blocking; drops if full).
 * line_len includes the trailing '\\n' if present.
 */
void csi_tcp_forward_enqueue(const char *line, size_t line_len);

bool csi_tcp_forward_enabled(void);

#ifdef __cplusplus
}
#endif
