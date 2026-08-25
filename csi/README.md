# csi

| Directory          | What                                               | Built by   |
| ------------------ | -------------------------------------------------- | ---------- |
| `shared/csi-core/` | Header-only wire format + sequence-gap tracking    | both       |
| `collector/`       | Host-side UDP receiver (macOS)                     | CMake      |
| `firmware/`        | XIAO ESP32-C6 CSI capture (Arduino via pioarduino) | PlatformIO |

`shared/csi-core` is the single source of truth for the bytes on the wire.
The collector consumes it as the CMake `csi::core` INTERFACE target; the
firmware consumes the _same directory_ through PlatformIO
(`lib_deps = csi-core=symlink://../shared/csi-core`). A `static_assert` in
`firmware/src/main.cpp` pins `csi::kFrameLen` so the two sides cannot
silently drift. Rules for what may live in the shared lib are in
[shared/csi-core/README.md](shared/csi-core/README.md).

## Building

Host (collector + all unit tests):

```sh
cmake -S . -B build -G Ninja
cmake --build build
ctest --test-dir build --output-on-failure
```

To run:

```sh
./build-release/collector/csi-collector
```

Optimized host build: `cmake --workflow --preset release`.

Firmware (needs `firmware/include/secrets.h`, copied from
`secrets.h.example`):

```sh
pio run -e seeed_xiao_esp32c6 -d firmware            # build
pio run -e seeed_xiao_esp32c6 -d firmware -t upload  # flash
pio device monitor                                    # watch CSI_DATA lines
```

## Editing

Open `csi.code-workspace` (not the bare folder): CMake Tools drives the host
build from the root, and the PlatformIO extension drives `firmware/` as its
own workspace folder.

## Layout notes

- `firmware/` is deliberately outside the CMake tree; PlatformIO owns it.
- The `firmware/ <-> shared/` sibling layout is load-bearing: the symlink
  lib_dep resolves relative to `firmware/`.
- Other board experiments (S3 mic, S3 camera, C6 LCD) remain in the original
  `tmp/esp32` project; only the CSI target lives here.
