# ---------------------------------------------------------------------------
# csi-core — header-only, platform-independent. The single source of truth
# for the CSI wire format, shared between the host collector (this CMake
# tree) and the ESP32 firmware (PlatformIO consumes the same directory via
# library.json + a symlink:// lib_dep; it ignores this file entirely).
#
# INTERFACE = nothing to compile; consumers inherit the include path and the
# C++20 requirement.
# ---------------------------------------------------------------------------
add_library(csi_core INTERFACE)
add_library(csi::core ALIAS csi_core)

target_include_directories(csi_core INTERFACE
    "${CMAKE_CURRENT_SOURCE_DIR}/include")
target_compile_features(csi_core INTERFACE cxx_std_20)

# ---------------------------------------------------------------------------
# Tests: host-only, guarded so this file is inert if included without the
# top-level testing infra. PlatformIO never compiles a library's test/ dir
# (and library.json export-excludes it), so Catch2 stays invisible on-device.
# ---------------------------------------------------------------------------
if(BUILD_TESTING AND TARGET Catch2::Catch2WithMain)
    file(GLOB CSI_CORE_TEST_SOURCES CONFIGURE_DEPENDS
        "${CMAKE_CURRENT_SOURCE_DIR}/test/*.cpp")

    add_executable(csi_core_tests ${CSI_CORE_TEST_SOURCES})
    target_link_libraries(csi_core_tests PRIVATE csi::core Catch2::Catch2WithMain)
    target_compile_options(csi_core_tests PRIVATE ${CSI_WARNINGS})
    catch_discover_tests(csi_core_tests)
endif()
