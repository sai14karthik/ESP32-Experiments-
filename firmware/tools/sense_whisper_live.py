#!/usr/bin/env python3
"""Live speech-to-text from Sense PCM (capture never blocks on Whisper).

Sources (prefer lowest latency):
  --pcm-udp 19055           raw s16le tee from ffmpeg_sense_av (no MediaMTX/AAC)
  --url  http://<esp>/audio raw Sense /audio (only if MediaMTX is not using it)
  --rtsp rtsp://…/cam_sense MediaMTX AAC path (extra remux delay — avoid)

Backend (Apple Silicon):
  auto → mlx-whisper (Metal via MLX) → openai-whisper MPS → CPU
  MLX is the fast/accurate path on Mac Mini; openai MPS often falls back to CPU.

Capture → WebRTC VAD (+ early partials) → mlx-whisper (Metal) → text.
Final phrases are speaker-labeled (YOU / OTHER_N) via Resemblyzer embeddings.
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
    "so",
    "the",
    "no",
}

# Filler loops Whisper invents on room noise / distant wall mics
_HALLUCINATION_PHRASES = (
    "i'm going to go ahead",
    "i am going to go ahead",
    "going to go ahead and get",
    "thanks for watching",
    "please subscribe",
    "see you in the next",
)


def _clean_text(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    low = t.lower().rstrip(".!")
    if low in _HALLUCINATIONS:
        return ""
    # long hum / stutter loops (Ummm…, aaaa…)
    letters = [c for c in low if c.isalpha()]
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
    """WebRTC VAD (preferred) + energy fallback, pre-roll, early partials.

    Research (mlx-audio / live STT): emit a partial ~1.5s into speech, finalize
    after ~0.3–0.5s silence. WebRTC VAD beats raw energy for real speech.
    """

    def __init__(
        self,
        threshold_db: float = -50.0,
        hangover_ms: int = 400,
        min_speech_ms: int = 450,
        max_speech_ms: int = 5000,
        preroll_ms: int = 300,
        partial_ms: int = 1800,
        webrtc_mode: int = 2,  # 0–3; 2 = stricter (less room-noise triggers)
        require_both: bool = True,  # webrtc AND energy (cuts wall-mic hallucinations)
    ):
        self.threshold_db = threshold_db
        self.require_both = require_both
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


def capture_loop(
    pcm_iter: Iterator[bytes],
    seg_q: queue.Queue,
    stop: threading.Event,
    threshold_db: float,
    source: str = "",
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
        vad = LiveVad(threshold_db=threshold_db)
        socks.append(sock)
        sock_meta[sock.fileno()] = (port, label, vad, bytearray(), [0, time.time()])
        print(f"[capture] listening udp://{host}:{port} ({label})", file=sys.stderr, flush=True)

    if not socks:
        print("[capture] no UDP ports bound", file=sys.stderr, flush=True)
        return

    print(
        f"[vad] {next(iter(sock_meta.values()))[2]._vad_name} (partials @ 1.5s) ×{len(socks)} sources",
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


class SpeakerTracker:
    """Online speaker IDs via Resemblyzer embeddings (YOU vs OTHER / SPEAKER_N).

    Whisper alone cannot tell speakers apart — this labels each *final* phrase.
    Best when people take turns (overlap is hard on one mic).
    """

    def __init__(
        self,
        max_speakers: int = 2,
        sim_threshold: float = 0.60,
        enroll_you: Optional[Path] = None,
    ):
        from resemblyzer import VoiceEncoder, preprocess_wav

        self._preprocess_wav = preprocess_wav
        self._encoder = VoiceEncoder()
        self.max_speakers = max(1, max_speakers)
        self.sim_threshold = sim_threshold
        self._names: list[str] = []
        self._centroids: list[np.ndarray] = []
        self._counts: list[int] = []
        self._last_name = "YOU"
        self._min_new_samples = SAMPLE_RATE  # ≥1.0s before creating OTHER_*
        if enroll_you is not None:
            path = Path(enroll_you).expanduser()
            wav = self._preprocess_wav(path)
            emb = self._encoder.embed_utterance(wav)
            self._names.append("YOU")
            self._centroids.append(emb.astype(np.float64))
            self._counts.append(1)
            self._last_name = "YOU"
            print(f"[diarize] enrolled YOU from {path}", flush=True)
        print(
            f"[diarize] on (max {self.max_speakers} speakers, sim≥{self.sim_threshold})",
            flush=True,
        )

    def _embed(self, audio_f32: np.ndarray) -> Optional[np.ndarray]:
        if audio_f32.size < SAMPLE_RATE // 2:  # <0.5s — too short for a reliable embed
            return None
        try:
            wav = self._preprocess_wav(audio_f32, source_sr=SAMPLE_RATE)
            if wav.size < SAMPLE_RATE // 2:
                return None
            return self._encoder.embed_utterance(wav).astype(np.float64)
        except Exception as e:
            print(f"[diarize] embed failed: {e}", file=sys.stderr, flush=True)
            return None

    @staticmethod
    def _cosine(a: np.ndarray, b: np.ndarray) -> float:
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na < 1e-9 or nb < 1e-9:
            return 0.0
        return float(np.dot(a, b) / (na * nb))

    def label(self, audio_f32: np.ndarray) -> str:
        emb = self._embed(audio_f32)
        # Short "yeah"/"ok" — keep current speaker (don't invent UNKNOWN/OTHER)
        if emb is None:
            return self._last_name

        best_i = -1
        best_sim = -1.0
        for i, c in enumerate(self._centroids):
            sim = self._cosine(emb, c)
            if sim > best_sim:
                best_sim = sim
                best_i = i

        if best_i >= 0 and best_sim >= self.sim_threshold:
            n = self._counts[best_i]
            self._centroids[best_i] = (self._centroids[best_i] * n + emb) / (n + 1)
            self._counts[best_i] = n + 1
            self._last_name = self._names[best_i]
            return self._last_name

        # Not enough audio / room for a new identity → stick with nearest or last
        can_add = (
            len(self._centroids) < self.max_speakers
            and audio_f32.size >= self._min_new_samples
        )
        if not can_add:
            if best_i >= 0:
                n = self._counts[best_i]
                self._centroids[best_i] = (self._centroids[best_i] * n + emb) / (n + 1)
                self._counts[best_i] = n + 1
                self._last_name = self._names[best_i]
                return self._last_name
            return self._last_name

        if not self._names:
            name = "YOU"
        elif "YOU" in self._names:
            name = f"OTHER_{len(self._names)}"
        else:
            name = f"SPEAKER_{len(self._names) + 1}"
        self._names.append(name)
        self._centroids.append(emb)
        self._counts.append(1)
        self._last_name = name
        print(f"[diarize] new voice → {name}", file=sys.stderr, flush=True)
        return name


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
    max_speakers: int,
    enroll_you: Optional[Path],
    sim_threshold: float,
) -> None:
    t_load = time.time()
    openai_model = None
    mlx_repo = ""
    speakers: Optional[SpeakerTracker] = None

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
            speakers = SpeakerTracker(
                max_speakers=max_speakers,
                sim_threshold=sim_threshold,
                enroll_you=enroll_you,
            )
        except Exception as e:
            print(f"[diarize] disabled ({e})", file=sys.stderr, flush=True)
            speakers = None

    last_partial = ""
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

            who = ""
            if is_final and speakers is not None:
                who = speakers.label(_pcm_to_float32(seg))

            ts = datetime.now().strftime("%H:%M:%S")
            ms = (time.time() - t0) * 1000
            src_tag = f"[{source}] " if source else ""
            if is_final:
                last_partial = ""
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
        help="Label speakers (YOU / OTHER_N). Default on. Disable with --no-diarize",
    )
    ap.add_argument("--max-speakers", type=int, default=2, help="Max distinct voices (default 2: YOU+OTHER)")
    ap.add_argument(
        "--enroll-you",
        type=Path,
        help="WAV/MP3 of YOUR voice (5–20s) → label YOU; others become OTHER_1…",
    )
    ap.add_argument(
        "--speaker-sim",
        type=float,
        default=0.60,
        help="Cosine similarity to reuse a speaker id (default 0.60; raise if two people merge)",
    )
    args = ap.parse_args()

    backend = _pick_backend(args.backend)
    openai_device = _pick_openai_device(args.device) if backend == "openai" else "n/a"
    stop = threading.Event()
    seg_q: queue.Queue = queue.Queue(maxsize=max(1, args.queue))

    if args.url:
        pcm_iter = iter_http_pcm(args.url, stop)
        print(f"[source] HTTP {args.url}", flush=True)
        capture = threading.Thread(
            target=capture_loop,
            args=(pcm_iter, seg_q, stop, args.vad_db, ""),
            name="capture",
            daemon=True,
        )
    elif args.rtsp:
        pcm_iter = iter_rtsp_pcm(args.rtsp, stop)
        print(f"[source] RTSP {args.rtsp}", flush=True)
        capture = threading.Thread(
            target=capture_loop,
            args=(pcm_iter, seg_q, stop, args.vad_db, ""),
            name="capture",
            daemon=True,
        )
    elif args.pcm_udp is not None:
        label = _port_label(args.pcm_udp)
        pcm_iter = iter_udp_pcm(args.pcm_udp, stop)
        print(f"[source] UDP pcm :{args.pcm_udp} ({label})", flush=True)
        capture = threading.Thread(
            target=capture_loop,
            args=(pcm_iter, seg_q, stop, args.vad_db, label),
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
            args=(ports, seg_q, stop, args.vad_db),
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
            args.max_speakers,
            args.enroll_you,
            args.speaker_sim,
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
