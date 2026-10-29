#!/bin/bash
# This file must be sourced so LD_LIBRARY_PATH is exported in the caller.

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    echo "Run: source scripts/install_trt_blackwell.sh" >&2
    exit 1
fi

sanitize_ld_preload() {
    local preload="" entry
    if [ -n "${LD_PRELOAD:-}" ]; then
        while IFS= read -r -d ':' entry || [ -n "$entry" ]; do
            if [ -n "$entry" ] && [ -e "$entry" ]; then
                preload="${preload:+$preload:}$entry"
            fi
        done <<< "$LD_PRELOAD"

        if [ -n "$preload" ]; then
            export LD_PRELOAD="$preload"
        else
            unset LD_PRELOAD
        fi
    fi
}

sanitize_ld_preload

if ! command -v nvidia-smi >/dev/null 2>&1; then
    return 0
fi

if [ "$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | cut -d. -f1-2)" != "12.0" ]; then
    return 0
fi

install_pytorch_blackwell() {
    if python3 -c 'from importlib.metadata import version
exit(0 if version("torch") == "2.7.1+cu128" and version("torchvision") == "0.22.1+cu128" else 1)' >/dev/null 2>&1; then
        return 0
    fi
    echo "[entrypoint] Installing PyTorch CUDA 12.8 wheels for Blackwell SM 12.0..." >&2
    python3 -m pip install \
        --index-url https://download.pytorch.org/whl/cu128 \
        "torch==2.7.1+cu128" \
        "torchvision==0.22.1+cu128"
}

install_pytorch_blackwell

install_nvidia_encoder() {
    local driver_version encode_candidate encode_extract_dir encode_deb
    driver_version="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
    find /usr/lib/x86_64-linux-gnu -name "libnvidia-encode.so.${driver_version}" -size +1M | grep -q . && return 0
    encode_candidate="$(apt-cache madison libnvidia-encode | awk -v version="${driver_version}" '$2 ~ "^"version {print $2; exit}')"
    [ -n "$encode_candidate" ] || encode_candidate="${driver_version}-1ubuntu1"
    encode_extract_dir="$(mktemp -d /tmp/nvidia-encode.XXXXXX)"
    (cd "$encode_extract_dir" && apt-get download "libnvidia-encode=${encode_candidate}" >/dev/null 2>&1) || return 1
    encode_deb="$(find "$encode_extract_dir" -maxdepth 1 -type f -name "libnvidia-encode_${encode_candidate}_amd64.deb" | head -1)"
    [ -n "$encode_deb" ] || return 1
    dpkg-deb -x "$encode_deb" "$encode_extract_dir" || return 1
    cp -a "$encode_extract_dir"/usr/lib/x86_64-linux-gnu/libnvidia-encode.so* /usr/lib/x86_64-linux-gnu/ || return 1
    rm -rf "$encode_extract_dir"
}

install_nvidia_decoder() {
    local driver_version decode_candidate decode_extract_dir decode_deb
    driver_version="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
    find /usr/lib/x86_64-linux-gnu -name "libnvcuvid.so.${driver_version}" -size +1M | grep -q . && return 0
    decode_candidate="$(apt-cache madison libnvidia-decode | awk -v version="${driver_version}" '$2 ~ "^"version {print $2; exit}')"
    [ -n "$decode_candidate" ] || decode_candidate="${driver_version}-1ubuntu1"
    decode_extract_dir="$(mktemp -d /tmp/nvidia-decode.XXXXXX)"
    (cd "$decode_extract_dir" && apt-get download "libnvidia-decode=${decode_candidate}" >/dev/null 2>&1) || return 1
    decode_deb="$(find "$decode_extract_dir" -maxdepth 1 -type f -name "libnvidia-decode_${decode_candidate}_amd64.deb" | head -1)"
    [ -n "$decode_deb" ] || return 1
    dpkg-deb -x "$decode_deb" "$decode_extract_dir" || return 1
    cp -a "$decode_extract_dir"/usr/lib/x86_64-linux-gnu/libnvcuvid.so* /usr/lib/x86_64-linux-gnu/ || return 1
    rm -rf "$decode_extract_dir"
}

install_rivermax_shim() {
    local target_library
    target_library=/usr/lib/x86_64-linux-gnu/librivermax.so.1
    if [ -e "$target_library" ] && nm -D --defined-only "$target_library" | grep -q rmax_init; then
        return 0
    fi
    rm -f "$target_library"
    [ -e ./scripts/rivermax_shim.c ] || return 1
    command -v gcc >/dev/null 2>&1 || return 1
    gcc -shared -fPIC -Wl,-soname,librivermax.so.1 \
        -o "$target_library" ./scripts/rivermax_shim.c
}

export_blackwell_python_library_path() {
    local python_nvidia_path
    python_nvidia_path=/usr/local/lib/python3.10/dist-packages/nvidia
    if [ ! -d "$python_nvidia_path/nvjitlink/lib" ]; then
        return 1
    fi
    export LD_LIBRARY_PATH="$python_nvidia_path/nvjitlink/lib:${LD_LIBRARY_PATH:-}"
}

install_rivermax_shim

if ! export_blackwell_python_library_path; then
    echo "[entrypoint] Missing cu128 libnvJitLink wheel; reinstall PyTorch CUDA 12.8 wheels." >&2
    return 1
fi

trtexec_version="$(trtexec --help 2>/dev/null | sed -n 's/.*TensorRT v\([0-9]*\).*/\1/p' | head -1)"
if [ "$trtexec_version" = "100800" ] || [ "$trtexec_version" = "10.8" ]; then
    install_nvidia_encoder
    install_nvidia_decoder
    install_rivermax_shim
    export LD_LIBRARY_PATH="/usr/local/nvidia/lib:/usr/local/nvidia/lib64:/lib/x86_64-linux-gnu:/usr/src/tensorrt/lib:${LD_LIBRARY_PATH}"
    return 0
fi

echo "[entrypoint] Installing TensorRT 10.8 for Blackwell SM 12.0..." >&2
apt-get update \
    -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/cuda-ubuntu2204-x86_64.list \
    -o Dir::Etc::sourceparts=- >/dev/null 2>&1 || return 1

DEBIAN_FRONTEND=noninteractive apt-get install -y \
    -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/cuda-ubuntu2204-x86_64.list \
    -o Dir::Etc::sourceparts=- \
    --allow-downgrades --allow-change-held-packages \
    tensorrt=10.8.0.43-1+cuda12.8 \
    libnvinfer-bin=10.8.0.43-1+cuda12.8 \
    libnvinfer-dev=10.8.0.43-1+cuda12.8 \
    libnvinfer-dispatch-dev=10.8.0.43-1+cuda12.8 \
    libnvinfer-dispatch10=10.8.0.43-1+cuda12.8 \
    libnvinfer-lean-dev=10.8.0.43-1+cuda12.8 \
    libnvinfer-lean10=10.8.0.43-1+cuda12.8 \
    libnvinfer-plugin-dev=10.8.0.43-1+cuda12.8 \
    libnvinfer-plugin10=10.8.0.43-1+cuda12.8 \
    libnvinfer-samples=10.8.0.43-1+cuda12.8 \
    libnvinfer-vc-plugin-dev=10.8.0.43-1+cuda12.8 \
    libnvinfer-vc-plugin10=10.8.0.43-1+cuda12.8 \
    libnvinfer10=10.8.0.43-1+cuda12.8 \
    libnvinfer-headers-dev=10.8.0.43-1+cuda12.8 \
    libnvinfer-headers-plugin-dev=10.8.0.43-1+cuda12.8 \
    libnvonnxparsers-dev=10.8.0.43-1+cuda12.8 \
    libnvonnxparsers10=10.8.0.43-1+cuda12.8 \
    python3-libnvinfer=10.8.0.43-1+cuda12.8 \
    python3-libnvinfer-dev=10.8.0.43-1+cuda12.8 \
    python3-libnvinfer-dispatch=10.8.0.43-1+cuda12.8 \
    python3-libnvinfer-lean=10.8.0.43-1+cuda12.8 >/dev/null 2>&1 || return 1

install_nvidia_encoder
install_nvidia_decoder
install_rivermax_shim
export LD_LIBRARY_PATH="/usr/local/nvidia/lib:/usr/local/nvidia/lib64:/lib/x86_64-linux-gnu:/usr/src/tensorrt/lib:${LD_LIBRARY_PATH}"
