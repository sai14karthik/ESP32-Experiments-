#pragma once

#include <string_view>

namespace csi {

// A tiny public header so the include/ wiring is exercised end-to-end:
// main.cpp pulls this in via "csi/banner.hpp".
inline constexpr std::string_view kBanner =
    "csi-collector — C++20 toolchain is wired up correctly.";

}  // namespace csi
