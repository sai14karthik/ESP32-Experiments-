#!/usr/bin/env python3
"""Live speech-to-text from Sense PCM (capture never blocks on Whisper).

Sources (prefer lowest latency):
  --pcm-udp 19055           raw s16le tee from ffmpeg_sense_av (no MediaMTX/AAC)
  --url  http://<esp>/audio raw Sense /audio (only if MediaMTX is not using it)
  --rtsp rtsp://…/cam_sense MediaMTX AAC path (extra remux delay — avoid)

Backend (Apple Silicon):
  auto → mlx-whisper (Metal via MLX) → openai-whisper MPS → CPU
  MLX is the fast/accurate path on Mac Mini; openai MPS often falls back to CPU.

Capture → energy VAD (pre-roll + short hangover) → queue → Whisper.
If the worker falls behind, oldest segments are dropped (prefer live speech).
"""

from __future__ import annotations

import argparse
import queue
import socket
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
DEFAULT_MODEL = "turbo"
DEFAULT_PCM_UDP_PORT = 19055

# openai-whisper name → mlx-community HF repo (Metal)
MLX_REPOS = {
    "turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3": "mlx-community/whisper-large-v3",
    "large": "mlx-community/whisper-large-v3",
    "medium.en": "mlx-community/whisper-medium.en",
    "medium": "mlx-community/whisper-medium",
    "small.en": "mlx-community/whisper-small.en",
    "small": "mlx-community/whisper-small",
    "base.en": "mlx-community/whisper-base.en",
    "base": "mlx-community/whisper-base",
    "tiny.en": "mlx-community/whisper-tiny.en",
    "tiny": "mlx-community/whisper-tiny",
}


def _pcm_to_float32(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def _rms_db(frame: np.ndarray) -> float:
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


def _mlx_available() -> bool:
    try:
        import mlx.core as mx  # noqa: F401
        import mlx_whisper  # noqa: F401

        # Apple Silicon only
        return True
    except Exception:
        return False


def _pick_backend(requested: str) -> str:
    """Return mlx | openai."""
    if requested == "mlx":
        if not _mlx_available():
            raise SystemExit(
                "mlx-whisper not available. On Mini: uv sync --group whisper"
            )
        return "mlx"
    if requested == "openai":
        return "openai"
    # auto
    if _mlx_available():
        return "mlx"
    return "openai"


def _pick_openai_device(requested: str) -> str:
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


def _mlx_repo(model_name: str) -> str:
    if model_name.startswith("mlx-community/") or model_name.startswith("./"):
        return model_name
    return MLX_REPOS.get(model_name, f"mlx-community/whisper-{model_name}")


# --- capture sources ---------------------------------------------------------


def iter_udp_pcm(port: int, stop: threading.Event, host: str = "127.0.0.1") -> Iterator[bytes]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.settimeout(0.5)
    print(f"[capture] listening udp://{host}:{port}", file=sys.stderr, flush=True)
    try:
        while not stop.is_set():
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as e:
                if stop.is_set():
                    break
                print(f"[capture] udp ({e})", file=sys.stderr, flush=True)
                time.sleep(0.2)
                continue
            if data:
                yield data
    finally:
        sock.close()


def iter_http_pcm(url: str, stop: threading.Event) -> Iterator[bytes]:
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
        "-map",
        "0:a:0",
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
    backoff = 0.5
    while not stop.is_set():
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert proc.stdout is not None
        assert proc.stderr is not None
        try:
            while not stop.is_set():
                chunk = proc.stdout.read(FRAME_BYTES * 4)
                if not chunk:
                    break
                backoff = 0.5
                yield chunk
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.communicate(timeout=2)
            except Exception:
                pass
        if stop.is_set():
            break
        print("[capture] rtsp reconnect", file=sys.stderr, flush=True)
        time.sleep(backoff)
        backoff = min(5.0, backoff * 1.5)


# --- VAD ---------------------------------------------------------------------


class EnergyVad:
    """Energy VAD with pre-roll (catch leading consonants) + short hangover (live)."""

    def __init__(
        self,
        threshold_db: float = -48.0,
        hangover_ms: int = 280,
        min_speech_ms: int = 350,
        max_speech_ms: int = 5000,
        preroll_ms: int = 250,
    ):
        self.threshold_db = threshold_db
        self.hangover_frames = max(1, hangover_ms // FRAME_MS)
        self.min_frames = max(1, min_speech_ms // FRAME_MS)
        self.max_frames = max(self.min_frames, max_speech_ms // FRAME_MS)
        self._preroll: Deque[bytes] = deque(maxlen=max(1, preroll_ms // FRAME_MS))
        self._in_speech = False
        self._silence_run = 0
        self._buf: Deque[bytes] = deque()

    def push(self, frame_bytes: bytes) -> Optional[bytes]:
        if len(frame_bytes) < FRAME_BYTES:
            return None
        n = (len(frame_bytes) // FRAME_BYTES) * FRAME_BYTES
        out_seg: Optional[bytes] = None
        for i in range(0, n, FRAME_BYTES):
            fb = frame_bytes[i : i + FRAME_BYTES]
            samples = np.frombuffer(fb, dtype=np.int16)
            db = _rms_db(samples)
            voiced = db >= self.threshold_db

            if not self._in_speech:
                self._preroll.append(fb)

            if voiced:
                self._silence_run = 0
                if not self._in_speech:
                    self._in_speech = True
                    self._buf.clear()
                    self._buf.extend(self._preroll)
                self._buf.append(fb)
                if len(self._buf) >= self.max_frames:
                    out_seg = b"".join(self._buf)
                    self._buf.clear()
                    self._in_speech = False
                    self._preroll.clear()
            elif self._in_speech:
                self._buf.append(fb)
                self._silence_run += 1
                if self._silence_run >= self.hangover_frames:
                    if len(self._buf) >= self.min_frames:
                        out_seg = b"".join(self._buf)
                    self._buf.clear()
                    self._in_speech = False
                    self._silence_run = 0
                    self._preroll.clear()
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


# --- Whisper workers ---------------------------------------------------------


def _transcribe_mlx(audio: np.ndarray, repo: str, language: str) -> str:
    import mlx_whisper

    result = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=repo,
        language=language,
        fp16=True,
        verbose=False,
        condition_on_previous_text=False,
        temperature=0.0,
        no_speech_threshold=0.5,
        compression_ratio_threshold=2.4,
        logprob_threshold=-0.8,
    )
    return (result.get("text") or "").strip()


def _transcribe_openai(model, audio_path: Path, language: str, device: str) -> str:
    result = model.transcribe(
        str(audio_path),
        language=language,
        fp16=(device == "cuda"),
        verbose=False,
        condition_on_previous_text=False,
        temperature=0.0,
    )
    return (result.get("text") or "").strip()


def whisper_worker(
    seg_q: queue.Queue,
    stop: threading.Event,
    model_name: str,
    language: str,
    backend: str,
    openai_device: str,
) -> None:
    t_load = time.time()
    openai_model = None
    mlx_repo = ""

    if backend == "mlx":
        mlx_repo = _mlx_repo(model_name)
        print(f"[whisper] loading MLX Metal model {mlx_repo} …", flush=True)
        # Warm-up: load weights once with a tiny silent buffer
        _transcribe_mlx(np.zeros(SAMPLE_RATE, dtype=np.float32), mlx_repo, language)
        print(
            f"[whisper] ready on Apple Metal (MLX) in {time.time() - t_load:.1f}s — speak near Sense mic",
            flush=True,
        )
    else:
        print(f"[whisper] loading openai-whisper {model_name} on {openai_device}…", flush=True)
        if openai_device == "cpu":
            print(
                "[whisper] WARNING: running on CPU (slow). Prefer: uv sync --group whisper (mlx-whisper)",
                file=sys.stderr,
                flush=True,
            )
        import whisper

        openai_model = whisper.load_model(model_name, device=openai_device)
        print(
            f"[whisper] ready on {openai_device} in {time.time() - t_load:.1f}s — speak near Sense mic",
            flush=True,
        )

    while not stop.is_set():
        try:
            seg = seg_q.get(timeout=0.2)
        except queue.Empty:
            continue
        if seg is None:
            break

        t0 = time.time()
        try:
            if backend == "mlx":
                text = _transcribe_mlx(_pcm_to_float32(seg), mlx_repo, language)
            else:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    path = Path(tmp.name)
                try:
                    write_wav(path, seg)
                    text = _transcribe_openai(openai_model, path, language, openai_device)
                finally:
                    path.unlink(missing_ok=True)

            if text:
                ts = datetime.now().strftime("%H:%M:%S")
                ms = (time.time() - t0) * 1000
                print(f"[{ts}] {text}  ({ms:.0f} ms)", flush=True)
        except Exception as e:
            print(f"[whisper] {e}", file=sys.stderr, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Live Whisper from Sense PCM — MLX Metal on Apple Silicon"
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--pcm-udp",
        type=int,
        metavar="PORT",
        help=f"Local UDP s16le tee (default port {DEFAULT_PCM_UDP_PORT})",
    )
    src.add_argument("--url", help="Sense PCM URL, e.g. http://10.128.93.25/audio")
    src.add_argument("--rtsp", help="MediaMTX RTSP (higher latency; prefer --pcm-udp)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"default: {DEFAULT_MODEL}")
    ap.add_argument("--language", default="en")
    ap.add_argument(
        "--backend",
        choices=("auto", "mlx", "openai"),
        default="auto",
        help="auto prefers mlx-whisper (Metal) on Apple Silicon",
    )
    ap.add_argument(
        "--device",
        default="auto",
        help="openai backend only: cpu | cuda | mps | auto",
    )
    ap.add_argument(
        "--vad-db",
        type=float,
        default=-48.0,
        help="Energy VAD dBFS (default -48; try -52 for quieter speech, -42 if noisy)",
    )
    ap.add_argument("--queue", type=int, default=2, help="Max pending segments (drop stale)")
    args = ap.parse_args()

    backend = _pick_backend(args.backend)
    openai_device = _pick_openai_device(args.device) if backend == "openai" else "n/a"
    stop = threading.Event()
    seg_q: queue.Queue = queue.Queue(maxsize=max(1, args.queue))

    if args.pcm_udp is not None:
        pcm_iter = iter_udp_pcm(args.pcm_udp, stop)
        print(f"[source] UDP pcm :{args.pcm_udp} (no MediaMTX lag)", flush=True)
    elif args.url:
        pcm_iter = iter_http_pcm(args.url, stop)
        print(f"[source] HTTP {args.url}", flush=True)
    else:
        pcm_iter = iter_rtsp_pcm(args.rtsp, stop)
        print(f"[source] RTSP {args.rtsp}", flush=True)

    print(f"[backend] {backend}" + (f" / {openai_device}" if backend == "openai" else " / Metal"), flush=True)

    worker = threading.Thread(
        target=whisper_worker,
        args=(seg_q, stop, args.model, args.language, backend, openai_device),
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
