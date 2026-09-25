#!/usr/bin/env python3
"""Record continuous Sense PCM (all audio, no VAD drop) to a WAV for accurate offline diarize.

Live captions (sense_whisper_live) may drop stale segments under load. This recorder
keeps every sample from the UDP tee so WhisperX / whisper-diarization can transcribe
+ diarize the full session accurately.

  ./scripts/sense_record_pcm.sh --ip 10.128.93.15 -o /tmp/sense_session.wav
  # Ctrl+C when done, then:
  ./scripts/sense_whisperx.sh /tmp/sense_session.wav
"""

from __future__ import annotations

import argparse
import select
import socket
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2

IP_TO_PORT: dict[str, int] = {
    "10.128.93.25": 19055,
    "10.128.93.34": 19056,
    "10.128.93.15": 19056,
}


def _resolve_port(ip: str | None, pcm_udp: int | None) -> tuple[int, str]:
    if pcm_udp is not None:
        return pcm_udp, f"udp:{pcm_udp}"
    addr = (ip or "10.128.93.15").strip()
    if addr not in IP_TO_PORT:
        raise SystemExit(f"unknown --ip {addr!r}; known: {sorted(IP_TO_PORT)} or use --pcm-udp")
    return IP_TO_PORT[addr], addr


def main() -> int:
    ap = argparse.ArgumentParser(description="Record Sense UDP PCM to WAV (full audio, no VAD)")
    ap.add_argument("--ip", default="10.128.93.15", help="Board IP (default 10.128.93.15)")
    ap.add_argument("--pcm-udp", type=int, help="Override UDP port")
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output WAV path (default: recordings/sense_YYYYMMDD_HHMMSS.wav)",
    )
    ap.add_argument("--host", default="127.0.0.1", help="UDP bind host")
    args = ap.parse_args()

    port, label = _resolve_port(args.ip, args.pcm_udp)
    out = args.output
    if out is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = Path("recordings") / f"sense_{stamp}_{label.replace('.', '_')}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, port))
    sock.setblocking(False)

    print(
        f"[record] listening udp://{args.host}:{port} ({label}) → {out}",
        flush=True,
    )
    print("[record] capturing ALL PCM (no VAD). Ctrl+C to stop & finalize WAV.", flush=True)

    bytes_in = 0
    t0 = time.time()
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(BYTES_PER_SAMPLE)
        wf.setframerate(SAMPLE_RATE)
        try:
            while True:
                readable, _, _ = select.select([sock], [], [], 0.5)
                if not readable:
                    continue
                try:
                    data, _ = sock.recvfrom(65535)
                except BlockingIOError:
                    continue
                if not data:
                    continue
                # Align to sample boundary
                n = (len(data) // BYTES_PER_SAMPLE) * BYTES_PER_SAMPLE
                if n <= 0:
                    continue
                chunk = data[:n]
                wf.writeframes(chunk)
                bytes_in += len(chunk)
                if bytes_in >= SAMPLE_RATE * BYTES_PER_SAMPLE * 5:
                    elapsed = max(0.001, time.time() - t0)
                    rate = bytes_in / BYTES_PER_SAMPLE / elapsed
                    dur = out.stat().st_size / (SAMPLE_RATE * BYTES_PER_SAMPLE) if out.exists() else 0
                    print(
                        f"[record] ~{rate:.0f} samples/s  file≈{dur:.1f}s",
                        file=sys.stderr,
                        flush=True,
                    )
                    bytes_in = 0
                    t0 = time.time()
        except KeyboardInterrupt:
            print("\n[record] stop — finalizing", flush=True)

    sock.close()
    samples = out.stat().st_size // BYTES_PER_SAMPLE
    print(
        f"[record] wrote {out} ({samples / SAMPLE_RATE:.1f}s). "
        f"Accurate diarize: ./scripts/sense_whisperx.sh {out}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
