// udp_sender.cpp — serialises CsiRecord → csi::Frame UDP datagram

#include "udp_sender.h"

#include <cstring>
#include <algorithm>

#include <Arduino.h>
#include <WiFi.h>
#include "secrets.h"
#include "csi_record.h"

#include <csi/frame.hpp>

#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "lwip/sockets.h"

// ── little-endian store (mirror of frame.hpp's load_u32_le) ───────────────
static void store_u32_le(std::byte* p, std::uint32_t v) noexcept {
    p[0] = std::byte(static_cast<std::uint8_t>(v));
    p[1] = std::byte(static_cast<std::uint8_t>(v >>  8));
    p[2] = std::byte(static_cast<std::uint8_t>(v >> 16));
    p[3] = std::byte(static_cast<std::uint8_t>(v >> 24));
}

// ── XOR-fold 6-byte MAC → single device_id ────────────────────────────────
static std::uint8_t macDeviceId() {
    uint8_t mac[6];
    WiFi.macAddress(mac);
    return mac[0] ^ mac[1] ^ mac[2] ^ mac[3] ^ mac[4] ^ mac[5];
}

// ── implementation ─────────────────────────────────────────────────────────
CsiUdpSender::CsiUdpSender() : fd_(-1), deviceId_{0} {}

CsiUdpSender::~CsiUdpSender() {
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }
}

bool CsiUdpSender::begin(const char* ip, uint16_t port) {
    deviceId_ = macDeviceId();

    fd_ = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd_ < 0) {
        Serial.printf("# UDP socket fail: errno %d\n", -fd_);
        return false;
    }

    sockaddr_in dst;
    dst.sin_family = AF_INET;
    dst.sin_port   = htons(port);
    dst.sin_addr.s_addr = inet_addr(ip);

    memcpy(dstBytes_, &dst, sizeof(dst));

    Serial.printf("# UDP sender started, device_id=0x%02x -> %s:%u\n",
                  deviceId_, ip, port);
    return true;
}

void CsiUdpSender::send(const CsiRecord& rec) {
    if (fd_ < 0) return;

    std::array<std::byte, csi::kFrameLen> wire{};

    wire[0] = std::byte(deviceId_);
    store_u32_le(wire.data() + csi::kDeviceIdLen, seq_++);

    std::memcpy(wire.data() + csi::kHeaderLen, rec.data,
                std::min<size_t>(rec.n, csi::kPayloadLen));

    ::sendto(fd_, wire.data(), wire.size(), 0,
             reinterpret_cast<sockaddr*>(dstBytes_), sizeof(dstBytes_));
    Serial.printf("#S \n");
}