#!/usr/bin/env python3
"""Live speech-to-text from Sense PCM (capture never blocks on Whisper).

Sources (prefer lowest latency):
  --pcm-udp 19055           raw s16le tee from ffmpeg_sense_av (no MediaMTX/AAC)
  --url  http://<esp>/audio raw Sense /audio (only if MediaMTX is not using it)
  --rtsp rtsp://…/cam_sense MediaMTX AAC path (extra remux delay — avoid)

Backend (Apple Silicon):
  auto → mlx-whisper (Metal via MLX) → openai-whisper MPS → CPU
  MLX is the fast/accurate path on Mac Mini; openai MPS often falls back to CPU.

Capture → WebRTC VAD (fallback energy) + early partials → queue → Whisper.
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

# Common Whisper garbage on short / quiet clips
_HALLUCINATIONS = {
    "",
    ".",
    "..",
    "...",
    "thank you",
    "thanks for watching",
    "thanks for watching.",
    "subscribe",
    "you",
    "bye",
    "okay",
    "ok",
    "um",
    "uh",
}


def _clean_text(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    if t.lower().rstrip(".!") in _HALLUCINATIONS:
        return ""
    # repetition loops (yeah yeah yeah…)
    words = t.lower().split()
    if len(words) >= 6 and len(set(words)) <= 2:
        return ""
    return t


class LiveVad:
    """WebRTC VAD (preferred) + energy fallback, pre-roll, early partials.

    Research (mlx-audio / live STT): emit a partial ~1.5s into speech, finalize
    after ~0.3–0.5s silence. WebRTC VAD beats raw energy for real speech.
    """

    def __init__(
        self,
        threshold_db: float = -48.0,
        hangover_ms: int = 300,
        min_speech_ms: int = 350,
        max_speech_ms: int = 5000,
        preroll_ms: int = 250,
        partial_ms: int = 1500,
        webrtc_mode: int = 2,
    ):
        self.threshold_db = threshold_db
        self.hangover_frames = max(1, hangover_ms // FRAME_MS)
        self.min_frames = max(1, min_speech_ms // FRAME_MS)
        self.max_frames = max(self.min_frames, max_speech_ms // FRAME_MS)
        self.partial_frames = max(1, partial_ms // FRAME_MS)
        self._preroll: Deque[bytes] = deque(maxlen=max(1, preroll_ms // FRAME_MS))
        self._in_speech = False
        self._silence_run = 0
        self._buf: Deque[bytes] = deque()
        self._partial_emitted = False
        self._webrtc = None
        try:
            # webrtcvad 2.0.10 imports deprecated pkg_resources; stub if missing
            import sys
            import types

            if "pkg_resources" not in sys.modules:
                try:
                    import pkg_resources  # noqa: F401
                except ImportError:
                    stub = types.ModuleType("pkg_resources")

                    class _Dist:
                        version = "2.0.10"

                    stub.get_distribution = lambda name: _Dist()  # type: ignore[attr-defined]
                    sys.modules["pkg_resources"] = stub
            import webrtcvad

            self._webrtc = webrtcvad.Vad(int(webrtc_mode))
            self._vad_name = f"webrtc:{webrtc_mode}"
        except Exception:
            self._vad_name = "energy"

    def _is_voiced(self, fb: bytes) -> bool:
        if self._webrtc is not None:
            try:
                return bool(self._webrtc.is_speech(fb, SAMPLE_RATE))
            except Exception:
                pass
        samples = np.frombuffer(fb, dtype=np.int16)
        return _rms_db(samples) >= self.threshold_db

    def push(self, frame_bytes: bytes) -> list[tuple[bytes, bool]]:
        """Return zero or more (pcm, is_final) segments."""
        out: list[tuple[bytes, bool]] = []
        if len(frame_bytes) < FRAME_BYTES:
            return out
        n = (len(frame_bytes) // FRAME_BYTES) * FRAME_BYTES
        for i in range(0, n, FRAME_BYTES):
            fb = frame_bytes[i : i + FRAME_BYTES]
            voiced = self._is_voiced(fb)

            if not self._in_speech:
                self._preroll.append(fb)

            if voiced:
                self._silence_run = 0
                if not self._in_speech:
                    self._in_speech = True
                    self._partial_emitted = False
                    self._buf.clear()
                    self._buf.extend(self._preroll)
                self._buf.append(fb)
                if (
                    not self._partial_emitted
                    and len(self._buf) >= self.partial_frames
                ):
                    out.append((b"".join(self._buf), False))
                    self._partial_emitted = True
                if len(self._buf) >= self.max_frames:
                    out.append((b"".join(self._buf), True))
                    self._buf.clear()
                    self._in_speech = False
                    self._partial_emitted = False
                    self._preroll.clear()
            elif self._in_speech:
                self._buf.append(fb)
                self._silence_run += 1
                if self._silence_run >= self.hangover_frames:
                    if len(self._buf) >= self.min_frames:
                        out.append((b"".join(self._buf), True))
                    self._buf.clear()
                    self._in_speech = False
                    self._silence_run = 0
                    self._partial_emitted = False
                    self._preroll.clear()
        return out


def _enqueue_seg(seg_q: queue.Queue, item: tuple[bytes, bool]) -> None:
    while True:
        try:
            seg_q.put_nowait(item)
            return
        except queue.Full:
            try:
                seg_q.get_nowait()
                print("[vad] drop stale segment", file=sys.stderr, flush=True)
            except queue.Empty:
                return


def capture_loop(
    pcm_iter: Iterator[bytes],
    seg_q: queue.Queue,
    stop: threading.Event,
    threshold_db: float,
) -> None:
    vad = LiveVad(threshold_db=threshold_db)
    print(f"[vad] {vad._vad_name} (partials @ 1.5s)", file=sys.stderr, flush=True)
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
            for item in vad.push(frame):
                _enqueue_seg(seg_q, item)
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
        condition_on_previous_text=False,  # avoids repetition loops on live chunks
        temperature=0.0,
        no_speech_threshold=0.6,
        compression_ratio_threshold=2.4,
        logprob_threshold=-1.0,
        hallucination_silence_threshold=0.4,
    )
    return _clean_text(result.get("text") or "")


def _transcribe_openai(model, audio_path: Path, language: str, device: str) -> str:
    result = model.transcribe(
        str(audio_path),
        language=language,
        fp16=(device == "cuda"),
        verbose=False,
        condition_on_previous_text=False,
        temperature=0.0,
    )
    return _clean_text(result.get("text") or "")


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

    last_partial = ""
    while not stop.is_set():
        try:
            item = seg_q.get(timeout=0.2)
        except queue.Empty:
            continue
        if item is None:
            break
        seg, is_final = item

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

            if not text:
                continue
            # Skip partial if identical to last (noise)
            if not is_final and text == last_partial:
                continue
            ts = datetime.now().strftime("%H:%M:%S")
            ms = (time.time() - t0) * 1000
            if is_final:
                last_partial = ""
                print(f"[{ts}] {text}  ({ms:.0f} ms)", flush=True)
            else:
                last_partial = text
                print(f"[{ts}] … {text}  ({ms:.0f} ms partial)", flush=True)
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
