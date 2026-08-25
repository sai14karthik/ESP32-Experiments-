# ---------------------------------------------------------------------------
# collector — the host-side UDP receiver. Subproject of the monorepo root,
# which owns the language settings, ${CSI_WARNINGS}, and the Catch2 setup.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Automatic source discovery
# ---------------------------------------------------------------------------
# We GLOB every .cpp under src/ so you can add a new file/class and just
# rebuild — no need to list sources here.
#
# Trade-off (the standard CMake caveat): globbing is not part of the build
# graph by default, so CMake can't *know* a new file appeared. Two mitigations
# are in play:
#   1. CONFIGURE_DEPENDS — tells CMake to re-run the glob at *build* time and
#      reconfigure automatically if the set of matched files changed. So adding
#      or deleting a .cpp triggers a re-glob on the next build, no manual step.
#      (Caveat: CONFIGURE_DEPENDS adds a small per-build check and is a CMake
#      promise rather than a hard guarantee across all generators — with Ninja
#      and Make on macOS it works well.)
#   2. If a new file is somehow missed, just re-run "CMake: Configure" in VS
#      Code (or `cmake -S . -B build`) to force a re-glob.
#
# Alternative worth knowing: explicitly listing sources in target_sources() is
# what upstream CMake officially recommends — it makes the dependency exact and
# survives edge cases. For a small, fast-moving starter project the convenience
# of glob+CONFIGURE_DEPENDS wins; switch to an explicit list once the file set
# stabilizes or if you hit a generator that ignores CONFIGURE_DEPENDS.
file(GLOB_RECURSE COLLECTOR_SOURCES CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp")

if(NOT COLLECTOR_SOURCES)
    message(FATAL_ERROR "No sources found under collector/src/ — add a .cpp file.")
endif()

# ---------------------------------------------------------------------------
# Core logic library (collector_core) — everything EXCEPT the entry point.
#
# Why a library at all? So the unit tests link the SAME compiled logic the
# executable runs, instead of recompiling src/ a second time (or not being able
# to reach it). main.cpp is held out because it defines main(), and the tests
# get their main() from Catch2::Catch2WithMain — two main()s collide at link.
#
# STATIC (a libcollector_core.a archive) is the simplest, most robust choice.
# To skip the archive step, change STATIC to OBJECT — nothing else changes.
# ---------------------------------------------------------------------------
set(COLLECTOR_MAIN "${CMAKE_CURRENT_SOURCE_DIR}/src/main.cpp")
set(COLLECTOR_LIB_SOURCES ${COLLECTOR_SOURCES})
list(REMOVE_ITEM COLLECTOR_LIB_SOURCES "${COLLECTOR_MAIN}")

add_library(collector_core STATIC ${COLLECTOR_LIB_SOURCES})

# include/ is a PUBLIC usage requirement: every target that links
# collector_core (the executable AND the tests) inherits this include path,
# so #include "csi/..." resolves everywhere without repeating it.
target_include_directories(collector_core PUBLIC
    "${CMAKE_CURRENT_SOURCE_DIR}/include")

# The wire format and seq tracker live in the shared header-only lib —
# the same headers the ESP32 firmware builds against. PUBLIC: anything
# linking collector_core sees them too.
target_link_libraries(collector_core PUBLIC csi::core)

# C++20, PUBLIC so consumers compile as C++20 too (propagates -std=c++20).
target_compile_features(collector_core PUBLIC cxx_std_20)
target_compile_options(collector_core PRIVATE ${CSI_WARNINGS})

# ---------------------------------------------------------------------------
# Executable — just the entry point, linked against the core library.
# ---------------------------------------------------------------------------
add_executable(csi-collector ${COLLECTOR_MAIN})
target_link_libraries(csi-collector PRIVATE collector_core)
target_compile_options(csi-collector PRIVATE ${CSI_WARNINGS})

# ---------------------------------------------------------------------------
# Host-side unit tests — conditional: git doesn't track an empty test/ dir,
# and its absence must not break the configure. Catch2 targets exist only
# when the root enabled BUILD_TESTING.
# ---------------------------------------------------------------------------
if(BUILD_TESTING AND TARGET Catch2::Catch2WithMain)
    file(GLOB_RECURSE COLLECTOR_TEST_SOURCES CONFIGURE_DEPENDS
        "${CMAKE_CURRENT_SOURCE_DIR}/test/*.cpp")

    if(COLLECTOR_TEST_SOURCES)
        add_executable(collector_tests ${COLLECTOR_TEST_SOURCES})
        target_link_libraries(collector_tests PRIVATE collector_core Catch2::Catch2WithMain)
        target_compile_options(collector_tests PRIVATE ${CSI_WARNINGS})
        catch_discover_tests(collector_tests)
    endif()
endif()
