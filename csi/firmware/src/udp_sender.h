#pragma once

#include <cstdint>

struct CsiRecord;

class CsiUdpSender {
    int fd_;
    uint8_t dstBytes_[16];  // sockaddr_in fits in 16 bytes
    std::uint8_t deviceId_;
    std::uint32_t seq_ = 0;  // monotonic per-sender; the collector's gap tracker relies on this
  public:
    CsiUdpSender();
    ~CsiUdpSender();
    bool begin(const char* ip, uint16_t port);
    void send(const CsiRecord& rec);
};