# csi-core

Header-only, platform-independent library shared between the host collector
(CMake, `csi::core` INTERFACE target) and the ESP32 firmware (PlatformIO,
`lib_deps = csi-core=symlink://../shared/csi-core`). It is the single source
of truth for the CSI wire format: if the two sides ever disagree on framing,
one of them is not building against this directory.

## Contents

- `include/csi/frame.hpp` — wire layout (`kFrameLen` = 1-byte device id +
  4-byte little-endian seq + 512-byte payload), `load_u32_le()`,
  `parse_frame()`, and the `Frame` value type.
- `include/csi/seq_tracker.hpp` — `SeqGapTracker`: per-device sequence-gap
  and reset detection.
- `test/` — Catch2 unit tests, built and run by the top-level CMake only
  (`ctest --test-dir build`). PlatformIO never compiles this directory.

## Rules for code in this library

- **Fixed-width types** (`std::uint8_t`, `std::uint32_t`) in anything
  wire-facing.
- **Byte order is explicit little-endian** via `load_u32_le` (and, when
  serialization lands, `store_u32_le`). Never `memcpy` an integer to or from
  the wire — that reproduces host byte order by accident.
- **No exceptions thrown, no RTTI reliance.** Everything `noexcept` and
  `constexpr` where possible; malformed input is a `std::nullopt`, not a
  throw.
- **Standard library only.** No POSIX, no Arduino, no OS headers. If a class
  needs a socket or a FreeRTOS queue, it belongs in `collector/` or
  `firmware/`, not here.
- **Dynamic allocation is deliberate.** `SeqGapTracker`'s `unordered_map`
  heap-allocates — fine on the host; on the ESP32 avoid instantiating it in
  ISR/CSI-callback paths, and consider a fixed-capacity variant if it is ever
  needed on-device. Headers you don't include cost nothing.
- `Frame` carries host receive timestamps (`std::chrono` time points). It
  compiles on the ESP32, but those are host semantics — firmware normally
  needs only the constants and (future) `serialize_frame()`.
