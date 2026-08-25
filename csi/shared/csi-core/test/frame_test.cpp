// frame_test.cpp — unit tests for the wire format: parse_frame + load_u32_le.
//
// Covers the header-only logic in csi/frame.hpp: decoding a valid datagram,
// rejecting malformed ones, and little-endian seq handling. No socket is
// involved — parse_frame is a pure function over a byte span.

#include <catch2/catch_test_macros.hpp>

#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <span>
#include <vector>

#include "csi/frame.hpp"

using namespace csi;

namespace {

// Assemble a well-formed datagram: [device_id][seq, little-endian][payload...].
// The payload is filled with a recognizable byte so we can assert it survives.
std::array<std::byte, kFrameLen> make_datagram(std::uint8_t device_id,
                                               std::uint32_t seq,
                                               std::uint8_t payload_fill = 0xAB) {
    std::array<std::byte, kFrameLen> d{};
    d[0] = std::byte{device_id};
    d[1] = static_cast<std::byte>(seq & 0xFFu);
    d[2] = static_cast<std::byte>((seq >> 8) & 0xFFu);
    d[3] = static_cast<std::byte>((seq >> 16) & 0xFFu);
    d[4] = static_cast<std::byte>((seq >> 24) & 0xFFu);
    for (std::size_t i = kHeaderLen; i < kFrameLen; ++i) {
        d[i] = std::byte{payload_fill};
    }
    return d;
}

std::span<const std::byte> as_span(const std::array<std::byte, kFrameLen>& a) {
    return std::span<const std::byte>(a.data(), a.size());
}

}  // namespace

TEST_CASE("parse_frame decodes a well-formed datagram", "[frame][parse]") {
    const std::chrono::steady_clock::time_point steady{std::chrono::seconds{42}};
    const std::chrono::system_clock::time_point wall{std::chrono::seconds{1000}};

    const auto bytes = make_datagram(/*device_id=*/7, /*seq=*/0x0A0B0C0Du, /*fill=*/0x5A);
    const auto frame = parse_frame(as_span(bytes), steady, wall);

    REQUIRE(frame.has_value());
    CHECK(frame->device_id == 7);
    CHECK(frame->seq == 0x0A0B0C0Du);

    SECTION("the capture timestamps are carried through unchanged") {
        CHECK(frame->recv_steady == steady);
        CHECK(frame->recv_wall == wall);
    }

    SECTION("the full fixed-size payload is copied out") {
        CHECK(frame->payload.size() == kPayloadLen);
        CHECK(frame->payload.front() == std::byte{0x5A});
        CHECK(frame->payload.back() == std::byte{0x5A});
    }
}

TEST_CASE("parse_frame rejects malformed datagrams", "[frame][parse][validation]") {
    const auto steady = std::chrono::steady_clock::now();
    const auto wall   = std::chrono::system_clock::now();

    SECTION("empty datagram") {
        const std::vector<std::byte> empty;
        CHECK_FALSE(parse_frame(std::span<const std::byte>(empty), steady, wall).has_value());
    }
    SECTION("header only, no payload") {
        const std::vector<std::byte> hdr(kHeaderLen, std::byte{0});
        CHECK_FALSE(parse_frame(std::span<const std::byte>(hdr), steady, wall).has_value());
    }
    SECTION("one byte too short") {
        const std::vector<std::byte> shortbuf(kFrameLen - 1, std::byte{0});
        CHECK_FALSE(parse_frame(std::span<const std::byte>(shortbuf), steady, wall).has_value());
    }
    SECTION("one byte too long (oversize must not be silently truncated)") {
        const std::vector<std::byte> longbuf(kFrameLen + 1, std::byte{0});
        CHECK_FALSE(parse_frame(std::span<const std::byte>(longbuf), steady, wall).has_value());
    }
}

TEST_CASE("seq is decoded little-endian", "[frame][endian]") {
    // load_u32_le assembles the value LSB-first regardless of host endianness.
    auto le = [](std::uint8_t b0, std::uint8_t b1, std::uint8_t b2, std::uint8_t b3) {
        const std::array<std::byte, 4> raw{std::byte{b0}, std::byte{b1},
                                           std::byte{b2}, std::byte{b3}};
        return load_u32_le(raw.data());
    };

    SECTION("each byte position carries the expected weight") {
        CHECK(le(0x01, 0x00, 0x00, 0x00) == 1u);
        CHECK(le(0x00, 0x01, 0x00, 0x00) == 256u);
        CHECK(le(0x00, 0x00, 0x01, 0x00) == 65536u);
        CHECK(le(0x00, 0x00, 0x00, 0x01) == 16777216u);
    }
    SECTION("mixed bytes assemble in little-endian order") {
        CHECK(le(0x78, 0x56, 0x34, 0x12) == 0x12345678u);
    }
    SECTION("all bits set is the unsigned max (no sign extension)") {
        CHECK(le(0xFF, 0xFF, 0xFF, 0xFF) == 0xFFFFFFFFu);
    }
    SECTION("the LE decode flows through parse_frame's seq field") {
        const auto bytes = make_datagram(/*device_id=*/1, /*seq=*/0x12345678u);
        const auto frame = parse_frame(as_span(bytes),
                                       std::chrono::steady_clock::now(),
                                       std::chrono::system_clock::now());
        REQUIRE(frame.has_value());
        CHECK(frame->seq == 0x12345678u);
    }
}

// load_u32_le is constexpr — prove it also folds at compile time.
namespace {
constexpr std::array<std::byte, 4> kLeBytes{std::byte{0x78}, std::byte{0x56},
                                            std::byte{0x34}, std::byte{0x12}};
static_assert(load_u32_le(kLeBytes.data()) == 0x12345678u,
              "load_u32_le must decode little-endian at compile time");
}  // namespace
