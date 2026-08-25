# ---------------------------------------------------------------------------
# csi — monorepo root for the host-side build.
#
# Owns everything project-wide: language settings, the shared warning set,
# and the testing infrastructure (Catch2 is fetched ONCE here so every
# subproject registers tests into one ctest run). Subprojects:
#
#   shared/csi-core   header-only, platform-independent (also consumed by
#                     the ESP32 firmware via PlatformIO — see firmware/)
#   collector         host UDP receiver executable + its tests
#
# firmware/ is deliberately NOT in this tree — PlatformIO owns it:
#   pio run -e seeed_xiao_esp32c6 -d firmware
# ---------------------------------------------------------------------------
# 3.20 gives reliable C++20 support, CONFIGURE_DEPENDS (3.12+), and presets.
cmake_minimum_required(VERSION 3.20)

project(csi
    VERSION 0.1.0
    DESCRIPTION "CSI monorepo: shared wire format + host collector"
    LANGUAGES CXX)

# ---------------------------------------------------------------------------
# Language standard: C++20, required, no compiler extensions (-std=c++20, not
# gnu++20). Applied per-target via target_compile_features in subprojects.
# ---------------------------------------------------------------------------
set(CMAKE_CXX_EXTENSIONS OFF)

# Emit compile_commands.json into the build dir. clangd and the VS Code C/C++
# extension use it for accurate IntelliSense without you maintaining a list.
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Guard against in-source builds: keep everything out-of-source in build/.
if(CMAKE_BINARY_DIR STREQUAL CMAKE_SOURCE_DIR)
    message(FATAL_ERROR
        "In-source builds are disabled. Configure into a separate dir, e.g.\n"
        "  cmake -S . -B build")
endif()

# ---------------------------------------------------------------------------
# Warnings — sensible defaults, consumed by subprojects via ${CSI_WARNINGS}.
# Applied PRIVATE to our own targets only — never to Catch2 or other deps.
# MSVC branch is just future-proofing; on macOS you'll hit the clang branch.
# ---------------------------------------------------------------------------
if(MSVC)
    set(CSI_WARNINGS /W4)
else()
    set(CSI_WARNINGS -Wall -Wextra -Wpedantic)
endif()

# ---------------------------------------------------------------------------
# Unit tests (Catch2 v3) — opt-in, hoisted to the root so the framework is
# fetched once and both subprojects' suites land in one ctest run.
#
# BUILD_TESTING is the conventional CMake switch for this, so a tests-off
# build is just:  cmake -S . -B build -DBUILD_TESTING=OFF
# When OFF, NONE of the block runs: no Catch2 download, no test targets
# (subprojects guard their test targets on Catch2::Catch2WithMain existing).
# ---------------------------------------------------------------------------
option(BUILD_TESTING "Build unit tests" ON)

if(BUILD_TESTING)
    enable_testing()  # turn on CTest for this (top-level) project

    # --- Catch2 v3 via FetchContent, pinned to an immutable release tag -----
    # Pinned to a TAG (not a branch) so every checkout builds the exact same
    # Catch2. GIT_SHALLOW fetches only that tag's commit. Bump deliberately.
    include(FetchContent)
    FetchContent_Declare(
        Catch2
        GIT_REPOSITORY https://github.com/catchorg/Catch2.git
        GIT_TAG        v3.15.1
        GIT_SHALLOW    TRUE
    )
    FetchContent_MakeAvailable(Catch2)

    # Compile Catch2 itself at C++20 — the SAME standard as our tests. Catch2's
    # CMake only requires C++14 and otherwise inherits the compiler DEFAULT, so
    # left alone its library builds below C++17. That omits Catch2's std::byte
    # StringMaker (gated on CATCH_CONFIG_CPP17_BYTE), and any test that prints a
    # std::byte on a failed assertion then fails to LINK with an undefined
    # StringMaker<std::byte>::convert. Matching the standards avoids that skew.
    target_compile_features(Catch2 PUBLIC cxx_std_20)

    # catch_discover_tests lives in Catch2's "extras" module dir. Making it
    # available here lets every subproject call it directly.
    list(APPEND CMAKE_MODULE_PATH "${catch2_SOURCE_DIR}/extras")
    include(Catch)
endif()

add_subdirectory(shared/csi-core)
add_subdirectory(collector)
