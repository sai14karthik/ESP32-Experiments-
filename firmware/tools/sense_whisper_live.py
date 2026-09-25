#!/usr/bin/env python3
"""Live speech-to-text from Sense PCM (capture never blocks on Whisper).

Sources (prefer lowest latency):
  --pcm-udp 19055           raw s16le tee from ffmpeg_sense_av (no MediaMTX/AAC)
  --url  http://<esp>/audio raw Sense /audio (only if MediaMTX is not using it)
  --rtsp rtsp://…/cam_sense MediaMTX AAC path (extra remux delay — avoid)

Backend (Apple Silicon):
  auto → mlx-whisper (Metal via MLX) → openai-whisper MPS → CPU
  MLX is the fast/accurate path on Mac Mini; openai MPS often falls back to CPU.

Capture → WebRTC VAD → mlx-whisper (Metal) → text (finals by default).

Speaker labels (live, Mini Metal) — pick one:
  ecapa (default)  SpeechBrain ECAPA gallery + live enroll YOU then OTHER.
                   Best for short live utterances (YOU vs person / YT).
  pyannote         HF community-1 embeddings gallery across clips (needs HF_TOKEN).
                   Still utterance-level; not full-file NeMo/WhisperX quality.

Do NOT wire MahmoudAshraf97/whisper-diarization into this live loop:
  that stack is offline (Demucs → faster-whisper → CTC align → NeMo MSDD/Sortformer),
  CUDA-first, seconds–minutes per file. Use scripts/sense_diarize_offline.sh on a
  recorded WAV / GPU box instead.

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
DEFAULT_PCM_UDP_PORT = 19056
DEFAULT_PCM_UDP_PORTS = tuple(range(19050, 19060))
# Board IP → Whisper UDP port (must match ffmpeg_sense_av.sh)
IP_TO_PORT: dict[str, int] = {
    "10.128.93.25": 19055,
    "10.128.93.34": 19056,
    "10.128.93.15": 19056,  # collar — DHCP moved off .34
}
PORT_TO_LABEL: dict[int, str] = {v: k for k, v in IP_TO_PORT.items()}

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


def _port_label(port: int) -> str:
    return PORT_TO_LABEL.get(port, f"udp:{port}")


def _ip_to_port(ip: str) -> int:
    ip = ip.strip()
    if ip.startswith("http://") or ip.startswith("https://"):
        ip = ip.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
    if ip in IP_TO_PORT:
        return IP_TO_PORT[ip]
    if ip.count(".") == 3:
        octet = ip.rsplit(".", 1)[-1]
        if octet.isdigit():
            return 19050 + (int(octet) % 10)
    raise SystemExit(f"Bad --ip {ip!r}. Example: --ip 10.128.93.34")


def _resolve_ips(spec: str) -> tuple[list[int], str]:
    """--ip 10.128.93.34 | --ip 10.128.93.25,10.128.93.34 | --ip all"""
    raw = spec.strip().lower()
    if raw in ("all", "both", "*"):
        ports = sorted(set(IP_TO_PORT.values()))
        return ports, "all known boards"
    parts = [p.strip() for p in spec.replace(";", ",").split(",") if p.strip()]
    ports: list[int] = []
    labels: list[str] = []
    for p in parts:
        port = _ip_to_port(p)
        ports.append(port)
        labels.append(_port_label(port))
    # unique preserve order
    seen: set[int] = set()
    uniq: list[int] = []
    for port in ports:
        if port not in seen:
            seen.add(port)
            uniq.append(port)
    return uniq, "+".join(labels)


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

# Common Whisper garbage on short / quiet / noisy clips
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
    "uhm",
    "uh huh",
    "so",
    "the",
    "no",
    "yeah",
    "yep",
    "yup",
    "hmm",
    "hm",
    "mmm",
    "mm",
    "mm-hmm",
    "mm hmm",
    "mhm",
    "ah",
    "oh",
    "huh",
}

# Filler loops Whisper invents on room noise / distant wall mics
_HALLUCINATION_PHRASES = (
    "i'm going to go ahead",
    "i am going to go ahead",
    "going to go ahead and get",
    "i'm going to go to the next one",
    "i am going to go to the next one",
    "going to go to the next one",
    "thanks for watching",
    "please subscribe",
    "see you in the next",
)


def _clean_text(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    low = t.lower().rstrip(".!,?")
    if low in _HALLUCINATIONS:
        return ""
    # Pure hum / filler (Mmm…, Uhhh…) — not real words
    letters = [c for c in low if c.isalpha()]
    if letters and len(set(letters)) <= 2 and all(c in "mnhuaeiou" for c in letters):
        return ""
    # long hum / stutter loops (Ummm…, aaaa…)
    if len(letters) >= 20 and len(set(letters)) <= 2:
        return ""
    for phrase in _HALLUCINATION_PHRASES:
        if phrase in low:
            if low.count(phrase) >= 1 and len(low) < 40 + 40 * low.count(phrase):
                return ""
            if low.count(phrase) >= 2:
                return ""
    words = t.lower().split()
    if len(words) >= 6 and len(set(words)) <= 2:
        return ""
    if len(words) >= 12:
        for n in (4, 5, 6, 7, 8):
            chunk = " ".join(words[:n])
            if chunk and t.lower().count(chunk) >= 3:
                return ""
    return t


def _segment_rms_db(pcm: bytes) -> float:
    if len(pcm) < 2:
        return -80.0
    return _rms_db(np.frombuffer(pcm, dtype=np.int16))


class LiveVad:
    """WebRTC VAD (preferred) + energy fallback, pre-roll, optional early partials.

    Default: finals only (no interim … lines). With partial_ms > 0, emit a
    draft ~1.5s into speech; finalize after hangover silence.
    """

    def __init__(
        self,
        threshold_db: float = -50.0,
        hangover_ms: int = 400,
        min_speech_ms: int = 450,
        max_speech_ms: int = 5000,
        preroll_ms: int = 300,
        partial_ms: int = 0,  # 0 = finals only (no double text)
        webrtc_mode: int = 2,  # 0–3; 2 = stricter (less room-noise triggers)
        require_both: bool = True,  # webrtc AND energy (cuts wall-mic hallucinations)
    ):
        self.threshold_db = threshold_db
        self.require_both = require_both
        self.hangover_frames = max(1, hangover_ms // FRAME_MS)
        self.min_frames = max(1, min_speech_ms // FRAME_MS)
        self.max_frames = max(self.min_frames, max_speech_ms // FRAME_MS)
        self.partial_frames = (
            max(1, partial_ms // FRAME_MS) if partial_ms > 0 else 0
        )
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
            self._vad_name = f"webrtc:{webrtc_mode}+energy"
        except Exception:
            self._vad_name = "energy"

    def _is_voiced(self, fb: bytes) -> bool:
        energy_ok = _rms_db(np.frombuffer(fb, dtype=np.int16)) >= self.threshold_db
        if self._webrtc is not None:
            try:
                speech = bool(self._webrtc.is_speech(fb, SAMPLE_RATE))
            except Exception:
                return energy_ok
            # AND: stops distant wall noise; collar still works if spoken near mic
            if self.require_both:
                return speech and energy_ok
            return speech or energy_ok
        return energy_ok

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
                    self.partial_frames > 0
                    and not self._partial_emitted
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


def _enqueue_seg(seg_q: queue.Queue, item: tuple) -> None:
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


class LoudestSourceGate:
    """When N boards are live, only keep segments from near-loudest mic (cuts wall hallucinations)."""

    def __init__(self, margin_db: float = 8.0, hold_s: float = 2.5):
        self.margin_db = margin_db
        self.hold_s = hold_s
        self._best_db = -80.0
        self._best_src = ""
        self._best_t = 0.0
        self._lock = threading.Lock()

    def allow(self, source: str, seg_db: float) -> bool:
        now = time.time()
        with self._lock:
            if now - self._best_t > self.hold_s:
                self._best_db = -80.0
                self._best_src = ""
            if seg_db >= self._best_db:
                self._best_db = seg_db
                self._best_src = source
                self._best_t = now
                return True
            # same source can continue a turn even if slightly quieter
            if source and source == self._best_src and seg_db >= self._best_db - 3.0:
                self._best_t = now
                return True
            if seg_db >= self._best_db - self.margin_db:
                return True
            return False


# Shared gate for multi-UDP capture (created in capture_loop_udp_ports)
_SOURCE_GATE: Optional[LoudestSourceGate] = None


def _vad_banner(vad: LiveVad) -> str:
    if vad.partial_frames > 0:
        return f"{vad._vad_name} (partials @ {vad.partial_frames * FRAME_MS}ms)"
    return f"{vad._vad_name} (finals only)"


def capture_loop(
    pcm_iter: Iterator[bytes],
    seg_q: queue.Queue,
    stop: threading.Event,
    threshold_db: float,
    source: str = "",
    partial_ms: int = 0,
) -> None:
    vad = LiveVad(threshold_db=threshold_db, partial_ms=partial_ms)
    print(f"[vad] {_vad_banner(vad)}", file=sys.stderr, flush=True)
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
            for pcm, is_final in vad.push(frame):
                _enqueue_seg(seg_q, (pcm, is_final, source))
        if bytes_in >= SAMPLE_RATE * BYTES_PER_SAMPLE * 5:
            elapsed = max(0.001, time.time() - t0)
            rate = bytes_in / BYTES_PER_SAMPLE / elapsed
            tag = f" {source}" if source else ""
            print(f"[capture]{tag} ~{rate:.0f} samples/s", file=sys.stderr, flush=True)
            bytes_in = 0
            t0 = time.time()


def capture_loop_udp_ports(
    ports: list[int],
    seg_q: queue.Queue,
    stop: threading.Event,
    threshold_db: float,
    host: str = "127.0.0.1",
    partial_ms: int = 0,
) -> None:
    """Listen on every Sense PCM tee — works for any cam_sense / cam_sense2 order."""
    import select

    global _SOURCE_GATE
    _SOURCE_GATE = LoudestSourceGate(margin_db=8.0, hold_s=2.5)

    socks: list[socket.socket] = []
    sock_meta: dict[int, tuple[int, str, LiveVad, bytearray, list]] = {}
    # fd → (port, label, vad, pending, [bytes_in, t0])

    for port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as e:
            print(f"[capture] skip :{port} ({e})", file=sys.stderr, flush=True)
            sock.close()
            continue
        sock.setblocking(False)
        label = _port_label(port)
        vad = LiveVad(threshold_db=threshold_db, partial_ms=partial_ms)
        socks.append(sock)
        sock_meta[sock.fileno()] = (port, label, vad, bytearray(), [0, time.time()])
        print(f"[capture] listening udp://{host}:{port} ({label})", file=sys.stderr, flush=True)

    if not socks:
        print("[capture] no UDP ports bound", file=sys.stderr, flush=True)
        return

    sample_vad = next(iter(sock_meta.values()))[2]
    print(
        f"[vad] {_vad_banner(sample_vad)} ×{len(socks)} sources",
        file=sys.stderr,
        flush=True,
    )

    try:
        while not stop.is_set():
            readable, _, _ = select.select(socks, [], [], 0.5)
            for sock in readable:
                meta = sock_meta.get(sock.fileno())
                if not meta:
                    continue
                port, label, vad, pending, stats = meta
                try:
                    while True:
                        data, _ = sock.recvfrom(8192)
                        if not data:
                            break
                        pending.extend(data)
                        stats[0] += len(data)
                except BlockingIOError:
                    pass
                except OSError as e:
                    print(f"[capture] udp :{port} ({e})", file=sys.stderr, flush=True)
                    continue
                while len(pending) >= FRAME_BYTES:
                    frame = bytes(pending[:FRAME_BYTES])
                    del pending[:FRAME_BYTES]
                    for pcm, is_final in vad.push(frame):
                        seg_db = _segment_rms_db(pcm)
                        # Drop near-silent / noise floor segments (Whisper invents filler)
                        if seg_db < threshold_db + 2.0:
                            continue
                        if _SOURCE_GATE is not None and not _SOURCE_GATE.allow(label, seg_db):
                            continue
                        _enqueue_seg(seg_q, (pcm, is_final, label))
                if stats[0] >= SAMPLE_RATE * BYTES_PER_SAMPLE * 5:
                    elapsed = max(0.001, time.time() - stats[1])
                    rate = stats[0] / BYTES_PER_SAMPLE / elapsed
                    print(f"[capture] {label} ~{rate:.0f} samples/s", file=sys.stderr, flush=True)
                    stats[0] = 0
                    stats[1] = time.time()
    finally:
        for sock in socks:
            sock.close()


# --- Speaker diarization (who spoke) ----------------------------------------


def _load_mono_16k(path: Path) -> np.ndarray:
    """Load enroll clip → float32 mono @ 16 kHz."""
    path = Path(path).expanduser()
    try:
        import torchaudio

        wav, sr = torchaudio.load(str(path))
        if wav.shape[0] > 1:
            wav = wav.mean(dim=0, keepdim=True)
        if int(sr) != SAMPLE_RATE:
            wav = torchaudio.functional.resample(wav, int(sr), SAMPLE_RATE)
        return wav.squeeze(0).numpy().astype(np.float32)
    except Exception:
        import wave

        with wave.open(str(path), "rb") as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise RuntimeError(f"enroll WAV must be mono s16le (or use torchaudio): {path}")
            sr = w.getframerate()
            pcm = w.readframes(w.getnframes())
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if sr != SAMPLE_RATE:
            # crude linear resample
            n = int(len(audio) * SAMPLE_RATE / sr)
            x = np.linspace(0, len(audio) - 1, n)
            audio = np.interp(x, np.arange(len(audio)), audio).astype(np.float32)
        return audio


class SpeakerTracker:
    """Speaker IDs via SpeechBrain ECAPA embeddings (far stronger than Resemblyzer).

    Best accuracy: enroll each person once from a clean WAV (5–20s on the same mic).
    Live enroll averages several utterances (~8s speech) before locking YOU/OTHER —
    a single ~3s clip is too noisy on a collar/Sense mic.
    """

    def __init__(
        self,
        max_speakers: int = 2,
        sim_threshold: float = 0.50,
        margin: float = 0.05,
        enrollments: Optional[dict[str, Path]] = None,
        enroll_live: bool = False,
        open_set: bool = False,
        enroll_seconds: float = 8.0,
    ):
        import torch
        from speechbrain.inference.speaker import EncoderClassifier

        self._torch = torch
        device = "cpu"
        if torch.backends.mps.is_available():
            # ECAPA encode is fine on CPU; MPS can be flaky for this model
            device = "cpu"
        self._encoder = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=str(Path.home() / ".cache" / "speechbrain" / "spkrec-ecapa-voxceleb"),
            run_opts={"device": device},
        )
        self.max_speakers = max(1, max_speakers)
        self.sim_threshold = sim_threshold
        self.margin = margin
        self.open_set = open_set
        self._names: list[str] = []
        self._centroids: list[np.ndarray] = []
        self._counts: list[int] = []
        self._last_name = "YOU"
        # Per-chunk floor vs total speech needed before locking a live identity
        self._min_enroll_chunk = int(SAMPLE_RATE * 1.2)
        self._enroll_need_samples = int(SAMPLE_RATE * max(4.0, float(enroll_seconds)))
        self._min_match_samples = int(SAMPLE_RATE * 0.8)
        self._enroll_live = enroll_live
        self._enroll_embs: list[np.ndarray] = []
        self._enroll_samples = 0
        self._pending_name = ""
        self._pending_hits = 0
        self._live_phase = "match"
        if enroll_live and not (enrollments or {}):
            self._live_phase = "enroll_you"
            need_s = self._enroll_need_samples / SAMPLE_RATE
            print(
                f"[diarize] LIVE ENROLL — speak clearly near the mic for ~{need_s:.0f}s "
                f"total (several phrases) to lock YOU",
                flush=True,
            )

        for name, path in (enrollments or {}).items():
            emb = self._embed(_load_mono_16k(path), min_samples=SAMPLE_RATE)
            if emb is None:
                raise RuntimeError(f"could not embed enroll clip for {name}: {path}")
            self._add(name, emb, announce=False)
            print(f"[diarize] enrolled {name} from {path}", flush=True)
            self._last_name = name

        if self._names and enroll_live and "YOU" in self._names and len(self._names) < self.max_speakers:
            self._live_phase = "enroll_other"
            self._reset_enroll_buf()
            need_s = self._enroll_need_samples / SAMPLE_RATE
            print(
                f"[diarize] LIVE ENROLL — OTHER person speak ~{need_s:.0f}s total to lock OTHER",
                flush=True,
            )
        elif self._names:
            self._live_phase = "match"

        backend = "speechbrain/ECAPA"
        print(
            f"[diarize] on ({backend}, max {self.max_speakers}, "
            f"sim≥{self.sim_threshold}, margin≥{self.margin}, "
            f"gallery={self._names or 'empty'})",
            flush=True,
        )

    def _reset_enroll_buf(self) -> None:
        self._enroll_embs = []
        self._enroll_samples = 0

    @staticmethod
    def _rms_db_f32(audio_f32: np.ndarray) -> float:
        if audio_f32.size == 0:
            return -80.0
        rms = float(np.sqrt(np.mean(np.square(audio_f32), dtype=np.float64)))
        return 20.0 * float(np.log10(max(rms, 1e-12)))

    @staticmethod
    def _normalize_level(audio_f32: np.ndarray, target_rms: float = 0.1) -> np.ndarray:
        """Match collar (loud) and desk/far (quiet) before ECAPA — identity, not distance."""
        x = np.ascontiguousarray(audio_f32, dtype=np.float32)
        rms = float(np.sqrt(np.mean(np.square(x), dtype=np.float64)))
        if rms < 1e-6:
            return x
        y = x * (target_rms / rms)
        peak = float(np.max(np.abs(y)))
        if peak > 0.95:
            y *= np.float32(0.95 / peak)
        return y

    def _embed(self, audio_f32: np.ndarray, min_samples: Optional[int] = None) -> Optional[np.ndarray]:
        need = min_samples if min_samples is not None else self._min_match_samples
        if audio_f32.size < need:
            return None
        try:
            normed = self._normalize_level(audio_f32)
            wav = self._torch.from_numpy(normed).float().unsqueeze(0)
            with self._torch.no_grad():
                emb = self._encoder.encode_batch(wav)
            v = emb.squeeze().detach().cpu().numpy().astype(np.float64)
            n = float(np.linalg.norm(v))
            if n < 1e-9:
                return None
            return v / n
        except Exception as e:
            print(f"[diarize] embed failed: {e}", file=sys.stderr, flush=True)
            return None

    @staticmethod
    def _cosine(a: np.ndarray, b: np.ndarray) -> float:
        return float(np.dot(a, b))  # both L2-normalized

    def _mean_emb(self, embs: list[np.ndarray]) -> Optional[np.ndarray]:
        if not embs:
            return None
        m = np.mean(np.stack(embs, axis=0), axis=0)
        n = float(np.linalg.norm(m))
        if n < 1e-9:
            return None
        return m / n

    def _add(self, name: str, emb: np.ndarray, announce: bool = True) -> str:
        self._names.append(name)
        self._centroids.append(emb)
        self._counts.append(1)
        self._last_name = name
        self._pending_name = ""
        self._pending_hits = 0
        if announce:
            print(f"[diarize] new voice → {name}", file=sys.stderr, flush=True)
        return name

    def _update(self, i: int, emb: np.ndarray) -> str:
        n = self._counts[i]
        # slow centroid move — avoid one odd clip dragging the identity
        self._centroids[i] = (self._centroids[i] * n + emb) / (n + 1)
        nn = float(np.linalg.norm(self._centroids[i]))
        if nn > 1e-9:
            self._centroids[i] /= nn
        self._counts[i] = n + 1
        self._last_name = self._names[i]
        self._pending_name = ""
        self._pending_hits = 0
        return self._last_name

    def _best_two(self, emb: np.ndarray) -> tuple[int, float, float]:
        best_i, best_sim, second = -1, -1.0, -1.0
        for i, c in enumerate(self._centroids):
            sim = self._cosine(emb, c)
            if sim > best_sim:
                second = best_sim
                best_sim = sim
                best_i = i
            elif sim > second:
                second = sim
        return best_i, best_sim, second

    def _confirm_or_stick(self, name: str, emb: np.ndarray, idx: int) -> str:
        """Need two consecutive votes to flip speaker — cuts collar/YT flip-flops."""
        if name == self._last_name:
            return self._update(idx, emb)
        if self._pending_name == name:
            self._pending_hits += 1
        else:
            self._pending_name = name
            self._pending_hits = 1
        if self._pending_hits >= 2:
            return self._update(idx, emb)
        return self._last_name

    def _accumulate_enroll(self, audio_f32: np.ndarray, label: str) -> Optional[np.ndarray]:
        """Average several clear phrases until ~enroll_seconds of speech."""
        if self._rms_db_f32(audio_f32) < -45.0:
            print(f"[diarize] enroll {label}: too quiet — speak louder / nearer mic", file=sys.stderr, flush=True)
            return None
        emb = self._embed(audio_f32, min_samples=self._min_enroll_chunk)
        if emb is None:
            return None
        self._enroll_embs.append(emb)
        self._enroll_samples += int(audio_f32.size)
        have = self._enroll_samples / SAMPLE_RATE
        need = self._enroll_need_samples / SAMPLE_RATE
        print(
            f"[diarize] enroll {label}: {have:.1f}/{need:.0f}s ({len(self._enroll_embs)} clips)",
            file=sys.stderr,
            flush=True,
        )
        if self._enroll_samples < self._enroll_need_samples or len(self._enroll_embs) < 2:
            return None
        return self._mean_emb(self._enroll_embs)

    def label(self, audio_f32: np.ndarray) -> str:
        if self._live_phase == "enroll_you":
            emb = self._accumulate_enroll(audio_f32, "YOU")
            if emb is None:
                return ""
            self._add("YOU", emb)
            self._reset_enroll_buf()
            if self.max_speakers >= 2:
                self._live_phase = "enroll_other"
                need_s = self._enroll_need_samples / SAMPLE_RATE
                print(
                    f"[diarize] YOU locked — OTHER person speak ~{need_s:.0f}s total "
                    f"(pause briefly first so VAD separates)",
                    flush=True,
                )
            else:
                self._live_phase = "match"
                print("[diarize] enrollment done — matching speakers", flush=True)
            return "YOU"

        if self._live_phase == "enroll_other":
            emb = self._accumulate_enroll(audio_f32, "OTHER")
            if emb is None:
                return self._last_name
            you_i = self._names.index("YOU") if "YOU" in self._names else 0
            you_sim = self._cosine(emb, self._centroids[you_i])
            # Stricter than match threshold — OTHER must clearly differ
            if you_sim >= self.sim_threshold - 0.08:
                print(
                    f"[diarize] still sounds like YOU (sim={you_sim:.2f}) — "
                    f"clear buffer, other person should speak alone",
                    file=sys.stderr,
                    flush=True,
                )
                self._reset_enroll_buf()
                return "YOU"
            self._add("OTHER", emb)
            self._reset_enroll_buf()
            self._live_phase = "match"
            print(
                f"[diarize] OTHER locked (vs YOU sim={you_sim:.2f}) — matching speakers",
                flush=True,
            )
            return "OTHER"

        emb = self._embed(audio_f32)
        if emb is None:
            return self._last_name

        if not self._names:
            if audio_f32.size < self._enroll_need_samples:
                return self._last_name
            return self._add("YOU", emb)

        best_i, best_sim, second = self._best_two(emb)
        confident = best_i >= 0 and best_sim >= self.sim_threshold and (
            second < 0 or (best_sim - second) >= self.margin
        )
        if confident:
            return self._confirm_or_stick(self._names[best_i], emb, best_i)

        # Soft stick — a bit looser so same voice at different distance still matches
        if best_i >= 0 and best_sim >= self.sim_threshold - 0.12:
            return self._confirm_or_stick(self._names[best_i], emb, best_i)

        if (
            self.open_set
            and len(self._centroids) < self.max_speakers
            and audio_f32.size >= self._enroll_need_samples
            and (best_i < 0 or best_sim < self.sim_threshold - 0.12)
        ):
            name = f"OTHER_{len(self._names)}" if "YOU" in self._names else f"SPEAKER_{len(self._names) + 1}"
            if name == "OTHER_1" and "OTHER" not in self._names:
                name = "OTHER"
            return self._add(name, emb)

        # Ambiguous — keep last speaker (turn continuity)
        return self._last_name


class PyannoteSpeakerTracker:
    """Speaker labels via pyannote embeddings (stable across short live clips).

    pyannote resets SPEAKER_00 on every call — we keep a cosine gallery of
    speaker_embeddings instead: first distinct voice → YOU, next → OTHER.
    Needs HF_TOKEN + accepted model terms on Hugging Face.
    """

    def __init__(
        self,
        max_speakers: int = 2,
        hf_token: Optional[str] = None,
        model_id: str = "pyannote/speaker-diarization-community-1",
        sim_threshold: float = 0.55,
        margin: float = 0.05,
    ):
        import os

        import torch
        from pyannote.audio import Pipeline

        token = (
            hf_token
            or os.environ.get("HF_TOKEN")
            or os.environ.get("HUGGINGFACE_HUB_TOKEN")
            or os.environ.get("HF_HUB_TOKEN")
        )
        if not token:
            try:
                from huggingface_hub import get_token

                token = get_token()
            except Exception:
                token = None
        if not token:
            raise RuntimeError(
                "pyannote needs HF_TOKEN — create at https://huggingface.co/settings/tokens "
                "and accept https://huggingface.co/pyannote/speaker-diarization-community-1"
            )

        self._torch = torch
        self.max_speakers = max(1, max_speakers)
        self.sim_threshold = sim_threshold
        self.margin = margin
        print(f"[diarize] loading {model_id} …", flush=True)
        try:
            self._pipeline = Pipeline.from_pretrained(model_id, token=token)
        except TypeError:
            self._pipeline = Pipeline.from_pretrained(model_id, use_auth_token=token)

        if torch.cuda.is_available():
            device = torch.device("cuda")
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            device = torch.device("mps")
        else:
            device = torch.device("cpu")
        try:
            self._pipeline.to(device)
        except Exception as e:
            print(f"[diarize] pipeline.to({device}) failed ({e}); staying on default", flush=True)
            device = torch.device("cpu")

        self._names: list[str] = []
        self._centroids: list[np.ndarray] = []
        self._counts: list[int] = []
        self._last_name = "YOU"
        self._min_samples = int(SAMPLE_RATE * 1.0)
        print(
            f"[diarize] on (pyannote @ {device}, max {self.max_speakers}; "
            f"embedding gallery, first voice→YOU, next→OTHER)",
            flush=True,
        )

    @staticmethod
    def _as_annotation(result):
        """pyannote.audio 4.x returns DiarizeOutput; older returns Annotation."""
        if result is None:
            return None
        ann = getattr(result, "speaker_diarization", None)
        if ann is not None and hasattr(ann, "itertracks"):
            return ann
        if hasattr(result, "itertracks"):
            return result
        return None

    @staticmethod
    def _norm_emb(v: np.ndarray) -> Optional[np.ndarray]:
        a = np.asarray(v, dtype=np.float64).reshape(-1)
        if a.size == 0 or not np.isfinite(a).all():
            return None
        n = float(np.linalg.norm(a))
        if n < 1e-9:
            return None
        return a / n

    def _add(self, name: str, emb: np.ndarray) -> str:
        self._names.append(name)
        self._centroids.append(emb)
        self._counts.append(1)
        self._last_name = name
        print(f"[diarize] new voice → {name}", file=sys.stderr, flush=True)
        return name

    def _update(self, i: int, emb: np.ndarray) -> str:
        n = self._counts[i]
        self._centroids[i] = (self._centroids[i] * n + emb) / (n + 1)
        nn = float(np.linalg.norm(self._centroids[i]))
        if nn > 1e-9:
            self._centroids[i] /= nn
        self._counts[i] = n + 1
        self._last_name = self._names[i]
        return self._last_name

    def _match(self, emb: np.ndarray) -> str:
        if not self._names:
            return self._add("YOU", emb)

        best_i, best_sim, second = -1, -1.0, -1.0
        for i, c in enumerate(self._centroids):
            sim = float(np.dot(emb, c))
            if sim > best_sim:
                second = best_sim
                best_sim = sim
                best_i = i
            elif sim > second:
                second = sim

        if best_i >= 0 and best_sim >= self.sim_threshold and (
            second < 0 or (best_sim - second) >= self.margin
        ):
            return self._update(best_i, emb)

        if best_i >= 0 and best_sim >= self.sim_threshold - 0.08:
            return self._update(best_i, emb)

        if len(self._names) < self.max_speakers and (
            best_i < 0 or best_sim < self.sim_threshold - 0.1
        ):
            name = "OTHER" if "YOU" in self._names and "OTHER" not in self._names else (
                f"OTHER_{len(self._names)}" if "YOU" in self._names else "YOU"
            )
            return self._add(name, emb)

        return self._last_name

    def _dominant_embedding(self, result) -> Optional[np.ndarray]:
        annotation = self._as_annotation(result)
        embeddings = getattr(result, "speaker_embeddings", None)
        if annotation is None or embeddings is None:
            return None
        dur: dict[str, float] = {}
        for turn, _, spk in annotation.itertracks(yield_label=True):
            dur[str(spk)] = dur.get(str(spk), 0.0) + float(turn.end - turn.start)
        if not dur:
            return None
        raw = max(dur, key=dur.get)
        labels = [str(x) for x in annotation.labels()]
        if raw not in labels:
            return None
        idx = labels.index(raw)
        emb_arr = np.asarray(embeddings)
        if emb_arr.ndim == 1:
            return self._norm_emb(emb_arr)
        if idx >= emb_arr.shape[0]:
            return None
        return self._norm_emb(emb_arr[idx])

    def label(self, audio_f32: np.ndarray) -> str:
        if audio_f32.size < self._min_samples:
            return self._last_name
        wav = np.ascontiguousarray(audio_f32, dtype=np.float32)
        # pyannote needs a little length; pad short finals lightly
        if wav.size < SAMPLE_RATE * 2:
            wav = np.pad(wav, (0, SAMPLE_RATE * 2 - wav.size))
        waveform = self._torch.from_numpy(wav).unsqueeze(0)
        try:
            kwargs: dict = {"min_speakers": 1, "max_speakers": self.max_speakers}
            if self.max_speakers == 1:
                kwargs = {"num_speakers": 1}
            with self._torch.inference_mode():
                result = self._pipeline(
                    {"waveform": waveform, "sample_rate": SAMPLE_RATE},
                    **kwargs,
                )
        except Exception as e:
            print(f"[diarize] pyannote failed: {e}", file=sys.stderr, flush=True)
            return self._last_name

        emb = self._dominant_embedding(result)
        if emb is None:
            return self._last_name
        return self._match(emb)


def make_speaker_tracker(
    backend: str,
    *,
    max_speakers: int,
    enrollments: Optional[dict[str, Path]],
    enroll_live: bool,
    sim_threshold: float,
    speaker_margin: float,
    open_set: bool,
    enroll_seconds: float = 8.0,
    hf_token: Optional[str] = None,
):
    b = (backend or "ecapa").lower()
    if b == "pyannote":
        return PyannoteSpeakerTracker(max_speakers=max_speakers, hf_token=hf_token)
    if b in ("ecapa", "speechbrain", "resemblyzer"):
        return SpeakerTracker(
            max_speakers=max_speakers,
            sim_threshold=sim_threshold,
            margin=speaker_margin,
            enrollments=enrollments or None,
            enroll_live=enroll_live,
            open_set=open_set,
            enroll_seconds=enroll_seconds,
        )
    raise ValueError(f"unknown --diarize-backend {backend!r} (use ecapa|pyannote)")


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
        no_speech_threshold=0.75,
        compression_ratio_threshold=2.2,
        logprob_threshold=-0.8,
        hallucination_silence_threshold=0.3,
    )
    segs = result.get("segments") or []
    if segs:
        # Drop if every segment looks like non-speech (Whisper still invents words)
        probs = [float(s.get("no_speech_prob") or 0.0) for s in segs]
        logps = [float(s.get("avg_logprob") or 0.0) for s in segs]
        if probs and min(probs) >= 0.6:
            return ""
        if logps and max(logps) < -1.0:
            return ""
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
    diarize: bool,
    diarize_backend: str,
    max_speakers: int,
    enrollments: dict[str, Path],
    enroll_live: bool,
    sim_threshold: float,
    speaker_margin: float,
    open_set: bool,
    enroll_seconds: float = 8.0,
) -> None:
    t_load = time.time()
    openai_model = None
    mlx_repo = ""
    speakers = None

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

    if diarize:
        try:
            speakers = make_speaker_tracker(
                diarize_backend,
                max_speakers=max_speakers,
                enrollments=enrollments or None,
                enroll_live=enroll_live,
                sim_threshold=sim_threshold,
                speaker_margin=speaker_margin,
                open_set=open_set,
                enroll_seconds=enroll_seconds,
            )
        except Exception as e:
            print(f"[diarize] disabled ({e})", file=sys.stderr, flush=True)
            speakers = None

    last_partial = ""
    last_final = ""
    last_final_t = 0.0
    while not stop.is_set():
        try:
            item = seg_q.get(timeout=0.2)
        except queue.Empty:
            continue
        if item is None:
            break
        if len(item) == 3:
            seg, is_final, source = item
        else:
            seg, is_final = item
            source = ""

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
            # Reject low-level noise that still slipped past VAD (hallucination fuel)
            if _segment_rms_db(seg) < -42.0 and is_final:
                continue
            if not is_final and text == last_partial:
                continue
            # Same caption again within 15s → almost always Whisper/YT loop, not new speech
            now = time.time()
            if is_final and text == last_final and (now - last_final_t) < 15.0:
                continue

            who = ""
            if is_final and speakers is not None:
                who = speakers.label(_pcm_to_float32(seg))

            ts = datetime.now().strftime("%H:%M:%S")
            ms = (time.time() - t0) * 1000
            src_tag = f"[{source}] " if source else ""
            if is_final:
                last_partial = ""
                last_final = text
                last_final_t = now
                who_tag = f"[{who}] " if who else ""
                print(f"[{ts}] {src_tag}{who_tag}{text}  ({ms:.0f} ms)", flush=True)
            else:
                last_partial = text
                print(f"[{ts}] {src_tag}… {text}  ({ms:.0f} ms partial)", flush=True)
        except Exception as e:
            print(f"[whisper] {e}", file=sys.stderr, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Live Whisper from Sense PCM — pick board with --ip"
    )
    src = ap.add_mutually_exclusive_group(required=False)
    src.add_argument(
        "--ip",
        metavar="ADDR",
        help="Board IP(s) for captions, e.g. 10.128.93.34 or 10.128.93.25,10.128.93.34 or all",
    )
    src.add_argument(
        "--pcm-udp",
        type=int,
        metavar="PORT",
        help="Raw UDP port override (advanced)",
    )
    src.add_argument("--url", help="Sense PCM URL, e.g. http://10.128.93.34/audio")
    src.add_argument("--rtsp", help="MediaMTX RTSP (higher latency; prefer --ip)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"default: {DEFAULT_MODEL}")
    ap.add_argument("--language", default="en")
    ap.add_argument(
        "--backend",
        choices=("auto", "mlx", "openai"),
        default="auto",
        help="auto prefers mlx-whisper (Metal) on Apple Silicon",
    )
    ap.add_argument(
        "--torch-device",
        default="auto",
        dest="device",
        help="openai backend only: cpu | cuda | mps | auto",
    )
    ap.add_argument(
        "--vad-db",
        type=float,
        default=-50.0,
        help="Energy VAD dBFS (default -50; try -55 quiet, -45 noisy)",
    )
    ap.add_argument("--queue", type=int, default=2, help="Max pending segments (drop stale)")
    ap.add_argument(
        "--diarize",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Speaker labels (default on). Backend: --diarize-backend",
    )
    ap.add_argument(
        "--diarize-backend",
        choices=("ecapa", "pyannote"),
        default="ecapa",
        help="Live labels: ecapa=ECAPA enroll/match (default, best on Mini); "
        "pyannote=HF embedding gallery (needs HF_TOKEN). "
        "For offline NeMo/whisper-diarization quality use scripts/sense_diarize_offline.sh",
    )
    ap.add_argument(
        "--partials",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Show interim … drafts before each final line (default: finals only)",
    )
    ap.add_argument("--max-speakers", type=int, default=2, help="Max distinct voices (default 2: YOU+OTHER)")
    ap.add_argument(
        "--enroll-you",
        type=Path,
        help="WAV of YOUR voice (5–20s clean speech) — ECAPA only",
    )
    ap.add_argument(
        "--enroll-other",
        type=Path,
        help="WAV of the other person's voice (5–20s) — ECAPA only",
    )
    ap.add_argument(
        "--enroll",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Enroll named speaker (ECAPA), e.g. --enroll YOU=~/me.wav",
    )
    ap.add_argument(
        "--enroll-live",
        action="store_true",
        help="ECAPA guided live enroll (ignored for pyannote)",
    )
    ap.add_argument(
        "--enroll-seconds",
        type=float,
        default=8.0,
        help="Live enroll: total clear speech seconds to lock each voice (default 8)",
    )
    ap.add_argument(
        "--open-set",
        action="store_true",
        help="ECAPA: allow inventing OTHER_N beyond enrolled gallery",
    )
    ap.add_argument(
        "--speaker-sim",
        type=float,
        default=0.50,
        help="ECAPA min cosine similarity (default 0.50; lower = more distance-tolerant)",
    )
    ap.add_argument(
        "--speaker-margin",
        type=float,
        default=0.05,
        help="ECAPA best-vs-2nd margin (default 0.05)",
    )
    args = ap.parse_args()
    partial_ms = 1800 if args.partials else 0

    enrollments: dict[str, Path] = {}
    for item in args.enroll:
        if "=" not in item:
            raise SystemExit(f"--enroll expects NAME=PATH, got: {item}")
        name, path = item.split("=", 1)
        enrollments[name.strip().upper()] = Path(path.strip())
    if args.enroll_you:
        enrollments.setdefault("YOU", args.enroll_you)
    if args.enroll_other:
        enrollments.setdefault("OTHER", args.enroll_other)

    # Live enroll only for ECAPA when no WAVs
    enroll_live = False
    if args.diarize and args.diarize_backend == "ecapa":
        enroll_live = bool(args.enroll_live) or not enrollments
        if enroll_live and not enrollments:
            print(
                "[diarize] no enroll WAVs — live enroll averages ~"
                f"{args.enroll_seconds:.0f}s speech per person "
                "(best accuracy: --enroll-you / --enroll-other WAVs)",
                flush=True,
            )
    elif args.diarize and args.diarize_backend == "pyannote":
        print(
            "[diarize] pyannote: embedding gallery — first distinct voice→YOU, next→OTHER "
            "(set HF_TOKEN; speak then play YT so voices differ)",
            flush=True,
        )

    backend = _pick_backend(args.backend)
    openai_device = _pick_openai_device(args.device) if backend == "openai" else "n/a"
    stop = threading.Event()
    seg_q: queue.Queue = queue.Queue(maxsize=max(1, args.queue))

    if args.url:
        pcm_iter = iter_http_pcm(args.url, stop)
        print(f"[source] HTTP {args.url}", flush=True)
        capture = threading.Thread(
            target=capture_loop,
            args=(pcm_iter, seg_q, stop, args.vad_db, "", partial_ms),
            name="capture",
            daemon=True,
        )
    elif args.rtsp:
        pcm_iter = iter_rtsp_pcm(args.rtsp, stop)
        print(f"[source] RTSP {args.rtsp}", flush=True)
        capture = threading.Thread(
            target=capture_loop,
            args=(pcm_iter, seg_q, stop, args.vad_db, "", partial_ms),
            name="capture",
            daemon=True,
        )
    elif args.pcm_udp is not None:
        label = _port_label(args.pcm_udp)
        pcm_iter = iter_udp_pcm(args.pcm_udp, stop)
        print(f"[source] UDP pcm :{args.pcm_udp} ({label})", flush=True)
        capture = threading.Thread(
            target=capture_loop,
            args=(pcm_iter, seg_q, stop, args.vad_db, label, partial_ms),
            name="capture",
            daemon=True,
        )
    else:
        # Default: wearable Sense 10.128.93.34 — override with --ip
        ip_spec = args.ip or "10.128.93.34"
        ports, desc = _resolve_ips(ip_spec)
        print(f"[source] --ip {ip_spec} → UDP {ports} ({desc})", flush=True)
        capture = threading.Thread(
            target=capture_loop_udp_ports,
            args=(ports, seg_q, stop, args.vad_db, "127.0.0.1", partial_ms),
            name="capture",
            daemon=True,
        )

    print(f"[backend] {backend}" + (f" / {openai_device}" if backend == "openai" else " / Metal"), flush=True)

    worker = threading.Thread(
        target=whisper_worker,
        args=(
            seg_q,
            stop,
            args.model,
            args.language,
            backend,
            openai_device,
            args.diarize,
            args.diarize_backend,
            args.max_speakers,
            enrollments,
            enroll_live if args.diarize else False,
            args.speaker_sim,
            args.speaker_margin,
            args.open_set,
            args.enroll_seconds,
        ),
        name="whisper",
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
