#!/usr/bin/env python3
"""Live speech-to-text from Sense PCM (smooth: capture never blocks on Whisper).

Sources:
  --url  http://<esp>/audio     raw s16le 16 kHz mono (Sense firmware)
  --rtsp rtsp://host:8554/cam_sense   when MediaMTX already owns /audio

Capture thread → energy VAD → queue → Whisper worker (default small.en).
If the worker falls behind, oldest pending segments are dropped (prefer fresh speech).
"""

from __future__ import annotations

import argparse
import queue
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Iterator, Optional
from urllib.request import urlopen

import numpy as np

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480
FRAME_BYTES = FRAME_SAMPLES * BYTES_PER_SAMPLE


def _pick_device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def _rms_db(frame: np.ndarray) -> float:
    # frame: int16
    x = frame.astype(np.float64)
    ms = float(np.mean(x * x))
    if ms < 1.0:
        return -80.0
    return 20.0 * np.log10(np.sqrt(ms) / 32768.0)


def write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(BYTES_PER_SAMPLE)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


# --- capture sources ---------------------------------------------------------


def iter_http_pcm(url: str, stop: threading.Event) -> Iterator[bytes]:
    """Chunked raw s16le from Sense GET /audio."""
    while not stop.is_set():
        try:
            with urlopen(url, timeout=15) as resp:
                while not stop.is_set():
                    chunk = resp.read(FRAME_BYTES * 4)
                    if not chunk:
                        break
                    yield chunk
        except Exception as e:
            print(f"[capture] http reconnect ({e})", file=sys.stderr, flush=True)
            time.sleep(0.5)


def iter_rtsp_pcm(rtsp_url: str, stop: threading.Event) -> Iterator[bytes]:
    """ffmpeg demux RTSP audio → raw s16le stdout."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
        "-vn",
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "pipe:1",
    ]
    while not stop.is_set():
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.stdout is not None
        try:
            while not stop.is_set():
                chunk = proc.stdout.read(FRAME_BYTES * 4)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        if stop.is_set():
            break
        print("[capture] rtsp reconnect", file=sys.stderr, flush=True)
        time.sleep(0.5)


# --- VAD + threads -----------------------------------------------------------


class EnergyVad:
    """Simple energy VAD with hangover (no webrtcvad required)."""

    def __init__(
        self,
        threshold_db: float = -42.0,
        hangover_ms: int = 450,
        min_speech_ms: int = 800,
        max_speech_ms: int = 8000,
    ):
        self.threshold_db = threshold_db
        self.hangover_frames = max(1, hangover_ms // FRAME_MS)
        self.min_frames = max(1, min_speech_ms // FRAME_MS)
        self.max_frames = max(self.min_frames, max_speech_ms // FRAME_MS)
        self._in_speech = False
        self._silence_run = 0
        self._buf: Deque[bytes] = deque()

    def push(self, frame_bytes: bytes) -> Optional[bytes]:
        if len(frame_bytes) < FRAME_BYTES:
            return None
        # use first FRAME_BYTES only for energy; keep full aligned frames in buf
        n = (len(frame_bytes) // FRAME_BYTES) * FRAME_BYTES
        out_seg: Optional[bytes] = None
        for i in range(0, n, FRAME_BYTES):
            fb = frame_bytes[i : i + FRAME_BYTES]
            samples = np.frombuffer(fb, dtype=np.int16)
            db = _rms_db(samples)
            voiced = db >= self.threshold_db

            if voiced:
                self._silence_run = 0
                if not self._in_speech:
                    self._in_speech = True
                    self._buf.clear()
                self._buf.append(fb)
                if len(self._buf) >= self.max_frames:
                    out_seg = b"".join(self._buf)
                    self._buf.clear()
                    self._in_speech = False
            elif self._in_speech:
                self._buf.append(fb)
                self._silence_run += 1
                if self._silence_run >= self.hangover_frames:
                    if len(self._buf) >= self.min_frames:
                        out_seg = b"".join(self._buf)
                    self._buf.clear()
                    self._in_speech = False
                    self._silence_run = 0
        return out_seg


def capture_loop(
    pcm_iter: Iterator[bytes],
    seg_q: queue.Queue,
    stop: threading.Event,
    threshold_db: float,
) -> None:
    vad = EnergyVad(threshold_db=threshold_db)
    pending = bytearray()
    bytes_in = 0
    t0 = time.time()
    while not stop.is_set():
        try:
            chunk = next(pcm_iter)
        except StopIteration:
            break
        except Exception as e:
            print(f"[capture] {e}", file=sys.stderr, flush=True)
            time.sleep(0.2)
            continue
        pending.extend(chunk)
        bytes_in += len(chunk)
        while len(pending) >= FRAME_BYTES:
            frame = bytes(pending[:FRAME_BYTES])
            del pending[:FRAME_BYTES]
            seg = vad.push(frame)
            if seg:
                # drop oldest if backlog
                while True:
                    try:
                        seg_q.put_nowait(seg)
                        break
                    except queue.Full:
                        try:
                            seg_q.get_nowait()
                            print("[vad] drop stale segment", file=sys.stderr, flush=True)
                        except queue.Empty:
                            pass
        if bytes_in >= SAMPLE_RATE * BYTES_PER_SAMPLE * 5:
            elapsed = max(0.001, time.time() - t0)
            rate = bytes_in / BYTES_PER_SAMPLE / elapsed
            print(f"[capture] ~{rate:.0f} samples/s", file=sys.stderr, flush=True)
            bytes_in = 0
            t0 = time.time()


def whisper_worker(
    seg_q: queue.Queue,
    stop: threading.Event,
    model_name: str,
    language: str,
    device: str,
) -> None:
    print(f"[whisper] loading {model_name} on {device}…", flush=True)
    import whisper

    model = whisper.load_model(model_name, device=device)
    print("[whisper] ready — speak near the Sense mic", flush=True)

    while not stop.is_set():
        try:
            seg = seg_q.get(timeout=0.3)
        except queue.Empty:
            continue
        if seg is None:
            break
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            path = Path(tmp.name)
        try:
            write_wav(path, seg)
            result = model.transcribe(
                str(path),
                language=language,
                fp16=(device == "cuda"),
                verbose=False,
            )
            text = (result.get("text") or "").strip()
            if text:
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] {text}", flush=True)
        except Exception as e:
            print(f"[whisper] {e}", file=sys.stderr, flush=True)
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Live Whisper from Sense /audio or cam_sense RTSP")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="Sense PCM URL, e.g. http://10.128.93.25/audio")
    src.add_argument("--rtsp", help="MediaMTX RTSP with audio, e.g. rtsp://10.128.93.23:8554/cam_sense")
    ap.add_argument("--model", default="small.en", help="Whisper model (default: small.en)")
    ap.add_argument("--language", default="en")
    ap.add_argument("--device", default="auto", help="cpu | cuda | mps | auto")
    ap.add_argument(
        "--vad-db",
        type=float,
        default=-42.0,
        help="Energy VAD threshold dBFS (raise if too sensitive, e.g. -38)",
    )
    ap.add_argument("--queue", type=int, default=3, help="Max pending speech segments")
    args = ap.parse_args()

    device = _pick_device(args.device)
    stop = threading.Event()
    seg_q: queue.Queue = queue.Queue(maxsize=max(1, args.queue))

    if args.url:
        pcm_iter = iter_http_pcm(args.url, stop)
        print(f"[source] HTTP {args.url}", flush=True)
    else:
        pcm_iter = iter_rtsp_pcm(args.rtsp, stop)
        print(f"[source] RTSP {args.rtsp}", flush=True)

    worker = threading.Thread(
        target=whisper_worker,
        args=(seg_q, stop, args.model, args.language, device),
        name="whisper",
        daemon=True,
    )
    capture = threading.Thread(
        target=capture_loop,
        args=(pcm_iter, seg_q, stop, args.vad_db),
        name="capture",
        daemon=True,
    )
    worker.start()
    capture.start()

    try:
        while capture.is_alive() and worker.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[stop]", flush=True)
    finally:
        stop.set()
        try:
            seg_q.put_nowait(None)
        except queue.Full:
            pass
        capture.join(timeout=2)
        worker.join(timeout=5)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
