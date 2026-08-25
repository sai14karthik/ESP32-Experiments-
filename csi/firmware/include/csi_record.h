#pragma once

#include <cstdint>

// CSI buffer max size — must match CSI_BUF_MAX in main.cpp
#define CSI_BUF_MAX 512

// ── one queued CSI sample (only fields that exist on the C6) ─────────────
struct CsiRecord {
  uint8_t  mac[6];
  int8_t   rssi;
  uint8_t  rate;
  int8_t   noise_floor;
  uint8_t  channel;
  uint8_t  second;          // secondary channel: 0 none / 1 above / 2 below
  uint8_t  bb_format;       // 0:11b 1:11g 2:HT 3:VHT 4:HE-SU 5:HE-MU 6:HE-ERSU 7:HE-TB
  uint8_t  single_mpdu;
  uint16_t sig_len;
  uint8_t  rx_state;
  uint32_t timestamp;       // local µs (wraps); precise only with power-save off
  uint16_t rx_seq;
  uint8_t  first_word_inv;
  uint16_t len;             // valid CSI byte count reported by the driver (subcarriers = len/2)
  uint16_t n;               // bytes actually copied into data[] (len minus skipped first word)
  int8_t   data[CSI_BUF_MAX];
};