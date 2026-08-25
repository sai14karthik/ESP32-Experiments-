#pragma once

// frame.hpp — the on-the-wire format and the in-memory Frame value type.
//
//Everything here is a plain value or a free function,
// so it can be unit-tested without opening a socket.

#include <array>
#include <chrono>
#include <cstddef>   // std::byte, std::size_t
#include <cstdint>
#include <cstring>   // std::memcpy
#include <optional>
#include <span>

namespace csi {

// ---------------------------------------------------------------------------
// Wire layout (one UDP datagram == one frame):
//
//   offset 0 : device_id   uint8                (1 byte)
//   offset 1 : seq         uint32 little-endian (4 bytes, monotonic per device)
//   offset 5 : payload     opaque CSI bytes     (kPayloadLen bytes)
//
// A datagram is valid iff its length is EXACTLY kHeaderLen + kPayloadLen.
// ---------------------------------------------------------------------------

// CSI payload size in bytes. Opaque to this layer
inline constexpr std::size_t kPayloadLen = 512;

inline constexpr std::size_t kDeviceIdLen = 1;
inline constexpr std::size_t kSeqLen      = 4;
inline constexpr std::size_t kHeaderLen   = kDeviceIdLen + kSeqLen;   // 5
inline constexpr std::size_t kFrameLen    = kHeaderLen + kPayloadLen; // header + payload

// ---------------------------------------------------------------------------
// Endianness-correct little-endian load.
//
// We assemble the value from individual bytes rather than memcpy-ing into a
// uint32_t, because memcpy reproduces the host byte order. Apple Silicon is
// little-endian so a memcpy would happen to be correct here — but only by
// accident. This byte assembly is correct on any host and documents intent;
// the compiler folds it to a single load (a no-op byteswap on little-endian).
constexpr std::uint32_t load_u32_le(const std::byte* p) noexcept {
    return  (static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(p[0]))      )
          | (static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(p[1])) <<  8)
          | (static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(p[2])) << 16)
          | (static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(p[3])) << 24);
}

// ---------------------------------------------------------------------------
// Frame — one decoded datagram, as handed to the consumer.
//
// A flat value type: copyable/movable, no ownership subtleties. Because the
// payload is a fixed-size std::array, "moving" a Frame copies its bytes — that
// is fine for the callback delivery path (built once, consumed once, no queue
// hops). If kPayloadLen ever grows large or frames get buffered deeply, switch
// `payload` to std::vector<std::byte> (cheap pointer-move) or a pooled buffer;
// the receiver interface does not change, since it only ever moves a Frame.
// ---------------------------------------------------------------------------
struct Frame {
    std::uint8_t  device_id = 0;
    std::uint32_t seq       = 0;

    // recv_steady: monotonic — the clock for jitter, gap intervals, sample rate.
    //              Never jumps; use this for any timing/arithmetic.
    std::chrono::steady_clock::time_point recv_steady{};

    // recv_wall: wall-clock — for logging and correlating with external systems.
    //            Can step backward (NTP/DST); never measure intervals with it.
    std::chrono::system_clock::time_point recv_wall{};

    // Opaque CSI bytes. std::byte (not char/uint8_t) says "raw bits, not text".
    std::array<std::byte, kPayloadLen> payload{};
};

// ---------------------------------------------------------------------------
// parse_frame — validate framing and decode, or reject.
//
// Pure and noexcept: takes the received bytes plus the two capture timestamps
// and returns a Frame, or std::nullopt if the datagram is malformed (wrong
// length). The caller treats nullopt as "drop and count" — a short, oversize,
// or empty datagram never reads out of bounds and never crashes.
// ---------------------------------------------------------------------------
inline std::optional<Frame> parse_frame(
        std::span<const std::byte> datagram,
        std::chrono::steady_clock::time_point recv_steady,
        std::chrono::system_clock::time_point recv_wall) noexcept {
    // Length check FIRST, before touching any field. Require exact size: the
    // payload is fixed, so anything shorter is truncated and anything longer is
    // unexpected — either way we cannot trust it.
    if (datagram.size() != kFrameLen) {
        return std::nullopt;
    }

    Frame f;
    f.device_id   = std::to_integer<std::uint8_t>(datagram[0]);
    f.seq         = load_u32_le(datagram.data() + kDeviceIdLen);  // explicit LE
    f.recv_steady = recv_steady;
    f.recv_wall   = recv_wall;
    std::memcpy(f.payload.data(), datagram.data() + kHeaderLen, kPayloadLen);
    return f;
}

}  // namespace csi
