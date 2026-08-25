# csi-collector

A clean, modern **C++20** starting point: CMake + automatic source discovery,
out-of-source builds, and one-button build/debug from VS Code on macOS
(Apple Silicon).

## Directory layout

```
csi-collector/
├── CMakeLists.txt        # Build: C++20, recursive source glob, warnings
├── README.md
├── .gitignore            # Excludes build/
├── include/              # Public headers, namespaced (include/csi/…)
│   └── csi/
│       └── banner.hpp
├── src/                  # All .cpp here are auto-discovered (globbed)
│   └── main.cpp          # Trivial program that prints + confirms C++20
└── .vscode/
    ├── settings.json     # CMake Tools: compiler, build dir, IntelliSense
    ├── tasks.json        # Default build task (Cmd+Shift+B)
    └── launch.json       # F5 debug via CodeLLDB
```

- **`src/`** — implementation files. Drop any new `.cpp` here (subfolders are
  fine, the glob is recursive) and rebuild — no need to edit `CMakeLists.txt`.
- **`include/`** — public headers, namespaced under `include/csi/` so includes
  read `#include "csi/banner.hpp"` and won't collide if you ever install them.
  Keep private/implementation-only headers next to their `.cpp` in `src/`.
- **`build/`** — generated, out-of-source, git-ignored. Safe to delete anytime.

## Prerequisites (status on this machine)

| Tool | Required | Status here |
|------|----------|-------------|
| CMake ≥ 3.20 | yes | ✅ 4.2.1 (`/opt/homebrew/bin/cmake`) |
| A C++20 compiler | yes | ✅ Apple clang 17 (`/usr/bin/clang++`) |
| lldb | for debugging | ✅ `/usr/bin/lldb` |
| Ninja | optional (faster builds) | ✅ 1.13.2 (`/opt/homebrew/bin/ninja`) |
| Homebrew LLVM clang | optional | ❌ not installed |

Already set up here. On a fresh machine:

```sh
brew install ninja        # CMake Tools auto-prefers Ninja once present
```

### VS Code extensions

You already have all of these installed; listed for completeness / other machines:

- **`ms-vscode.cmake-tools`** — CMake Tools (configure/build/debug UI) — primary.
- **`ms-vscode.cpptools`** — C/C++ IntelliSense.
- **`vadimcn.vscode-lldb`** — CodeLLDB (debugger used by `launch.json`).
- `twxs.cmake` — CMake syntax highlighting (nice to have).

## First-time configure

**VS Code (recommended flow):**
1. Open this folder in VS Code.
2. CMake Tools auto-configures on open (`cmake.configureOnOpen`).
3. If it asks **"Select a Kit"**, pick **`[Unspecified]`** — that tells CMake
   Tools to honor the compiler pinned in `.vscode/settings.json` (Apple clang)
   instead of an auto-scanned kit.

**Command line (equivalent):**
```sh
cmake -S . -B build              # add -G Ninja after `brew install ninja`
```

## Build

**One button / shortcut:** press **`Cmd+Shift+B`** (the default build task), or
click **Build** in the CMake Tools status bar at the bottom of the window.

**Command line:**
```sh
cmake --build build
./build/csi-collector
```
Expected output:
```
csi-collector — C++20 toolchain is wired up correctly.
__cplusplus = 202002
```

### Release build

The default flows above (VS Code button, `cmake -S . -B build`) configure with an
**empty `CMAKE_BUILD_TYPE`** — unoptimized, asserts on. That's right for
development. For a production/release binary (`-O3 -DNDEBUG`, tests excluded), use
the `release` preset in `CMakePresets.json`:

```sh
cmake --workflow --preset release      # configure + build in one command
./build-release/csi-collector          # the optimized binary
```

Details:
- Builds into a **separate `build-release/`** dir, so it never disturbs the debug
  `build/` cache — the default flow keeps working untouched (both dirs are
  git-ignored).
- `BUILD_TESTING=OFF` in the preset skips the Catch2 download and the test target.
- Two-step equivalent, if you prefer: `cmake --preset release && cmake --build --preset release`.

> **Why is this CLI-only?** `.vscode/settings.json` sets
> `"cmake.useCMakePresets": "never"` so CMake Tools keeps its classic one-button
> flow (the pinned build dir / compiler / generator). Without that, merely having
> `CMakePresets.json` present would flip the extension into preset mode. The preset
> is deliberately a command-line tool; the VS Code experience is unchanged.

## Tests

Unit tests use **Catch2 v3**, pulled in by CMake's `FetchContent` and pinned to
release tag **`v3.15.1`**. They live in `test/` and link the project's logic via
the `csi_core` library (see the `CMakeLists.txt` comments for why `main.cpp` is
held out of that library). Test sources are globbed like `src/` — drop a new
`*_test.cpp` into `test/` and rebuild.

**Build + run from the command line:**
```sh
cmake -S . -B build -G Ninja      # configures + downloads Catch2 into build/_deps
cmake --build build
ctest --test-dir build --output-on-failure
```
`ctest` runs each `TEST_CASE` as its own test (registered via
`catch_discover_tests`). To run the test binary directly (handy for Catch2 tag
filters and section output): `./build/csi_tests "[seq]"` or `./build/csi_tests --list-tests`.

**From inside VS Code:** the **CMake Tools** extension (`ms-vscode.cmake-tools`)
surfaces CTest tests in the **Testing panel** (the beaker icon) — configure and
build once, then run/debug individual cases from the tree. If they don't appear,
run **“CMake: Refresh Tests”** from the command palette. (The dedicated **C++
TestMate** extension, `matepek.vscode-catch2-test-explorer`, is an optional
alternative that discovers the Catch2 binary directly — not required here.)

**Turning tests off:** everything test-related is guarded by the standard
`BUILD_TESTING` option (default `ON`). A tests-off build skips the Catch2
download and the test target entirely:
```sh
cmake -S . -B build -DBUILD_TESTING=OFF
```

> **Catch2 is not committed.** `FetchContent` downloads it into `build/_deps/`,
> which is already covered by the `build/` entry in `.gitignore` — so there's
> nothing extra to ignore and nothing to vendor. Reproducibility comes from the
> pinned `GIT_TAG v3.15.1` in `CMakeLists.txt`: every checkout fetches that exact
> release. Bump the tag deliberately when you want a newer Catch2. (For
> bit-for-bit pinning you can use the tag's commit SHA instead —
> `a60dd32074f7698e88a40d1baa4eaeeb1232828f` — since a SHA can never move.)

## Debug

Press **`F5`**. It builds first (`preLaunchTask`), then launches under CodeLLDB.
The debugged binary is whatever target CMake Tools has selected
(`${command:cmake.launchTargetPath}`), so set a breakpoint in `src/main.cpp`
and step through.

> Why CodeLLDB and not the C/C++ extension's `cppdbg`? On Apple Silicon
> `cppdbg`'s lldb backend depends on the deprecated `lldb-mi`; CodeLLDB talks to
> lldb directly and is the reliable choice here.

## Adding a new file or class

Just create it under `src/` (and a header under `include/csi/`). For example a
`Greeter` class:

```
include/csi/greeter.hpp     // declaration
src/greeter.cpp             // definition  ← picked up by the glob
```

Then build. Because the glob uses `CONFIGURE_DEPENDS`, CMake notices the new
file and re-globs automatically on the next build. If it's ever missed, run
**CMake: Configure** in VS Code (or the `cmake: configure (CLI)` task) to force
a re-glob.

> **Globbing trade-off:** `file(GLOB_RECURSE … CONFIGURE_DEPENDS)` trades a hard
> guarantee for convenience. Upstream CMake officially recommends listing
> sources explicitly in `target_sources()` so the build graph is exact;
> `CONFIGURE_DEPENDS` closes most of the gap by re-globbing at build time, which
> works well with Make/Ninja on macOS. For a fast-moving starter this is the
> right default — switch to an explicit list once the file set stabilizes.

## Switching to Homebrew LLVM clang

The project defaults to **Apple clang** because Homebrew LLVM isn't installed
and Apple clang needs zero extra setup: it's an `xcrun` wrapper that selects the
macOS SDK itself (no `-isysroot` needed) and links the system libc++. To switch:

```sh
brew install llvm
```

Then in `.vscode/settings.json` replace the compiler line with the Homebrew one
(it's already there, commented):

```jsonc
"CMAKE_CXX_COMPILER": "/opt/homebrew/opt/llvm/bin/clang++"
```

…and **delete `build/`** before reconfiguring (CMake caches the compiler on
first configure, so it won't switch in place).

> **Apple-Silicon caveat for Homebrew LLVM:** the `llvm` formula is *keg-only*
> (not on `PATH` — hence the explicit `/opt/homebrew/opt/llvm/bin` path), and its
> clang has no built-in SDK, so CMake passes `-isysroot $(xcrun --show-sdk-path)`
> for it (the Command Line Tools still need to be installed). By default Homebrew
> clang links the **system** libc++/libunwind, so simple executables build fine
> with no extra flags — its *bundled* libc++ is opt-in only, and if you
> deliberately enable it you must add the `LDFLAGS` that `brew info llvm` prints.
> Apple clang sidesteps all of this, which is why it's the default.

## macOS / Apple-Silicon notes

- **Architecture:** everything builds native **arm64** — no Rosetta. The macOS
  SDK comes from `xcrun --show-sdk-path` (CMake derives the sysroot from it):
  Apple clang selects it automatically; for Homebrew clang CMake passes the
  `-isysroot` explicitly since that compiler has no default SDK.
- **Ninja vs Make:** Ninja is installed, so the **VS Code flow (CMake Tools)
  uses it automatically**. On the **command line**, plain `cmake` still defaults
  to Unix Makefiles regardless of Ninja being present — pass `-G Ninja` (or set
  `CMAKE_GENERATOR=Ninja`) to use it there. Switching the generator of an
  existing build needs a fresh `build/` dir.
- **Debugger:** uses CodeLLDB (system `lldb`). No code-signing dance needed for
  debugging your own locally-built binaries.
- **CMake 4.x:** `cmake_minimum_required(VERSION 3.20)` is set; CMake 4 dropped
  compatibility with declaring a minimum below 3.5, so don't lower it past that.
```
