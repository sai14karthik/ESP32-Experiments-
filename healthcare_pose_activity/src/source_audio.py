"""Sound that belongs to the same source as the picture.

A live RTSP/MJPEG URL is read into a short rolling buffer. A video file
contributes the few seconds of its own track that match the frame on screen.
A source with no sound track contributes nothing.
"""
from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from mjpeg import is_mjpeg_url, is_rtsp_url, is_video_file

RATE = 16000
# Recent sound sent with the current frame: inside the 5–10 s window.
CLIP_S = 8.0


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _ffprobe() -> str | None:
    return shutil.which("ffprobe")


def has_audio_track(source: str, *, timeout: float = 8.0) -> bool:
    """True when ``source`` has a sound track ffmpeg can read."""
    ffprobe = _ffprobe()
    if ffprobe:
        cmd = [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
        ]
        if is_rtsp_url(source):
            cmd[1:1] = ["-rtsp_transport", "tcp"]
        cmd.append(source)
        try:
            run = subprocess.run(
                cmd, capture_output=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return b"audio" in run.stdout

    ffmpeg = _ffmpeg()
    if not ffmpeg:
        return False
    cmd = [ffmpeg, "-nostdin", "-v", "error", "-t", "0.2"]
    if is_rtsp_url(source):
        cmd += ["-rtsp_transport", "tcp"]
    cmd += ["-i", source, "-map", "0:a:0", "-f", "null", "-"]
    try:
        run = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return run.returncode == 0 and b"matches no streams" not in run.stderr


def _encode_flac(args: list[str], pcm: bytes | None = None) -> bytes | None:
    """Write FLAC to a real file. A pipe has no duration, and the server rejects that."""
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        return None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "clip.flac")
            run = subprocess.run(
                [ffmpeg, "-nostdin", "-v", "error", *args, out],
                input=pcm,
                capture_output=True,
                timeout=8,
                check=False,
            )
            if run.returncode != 0 or not Path(out).is_file():
                return None
            data = Path(out).read_bytes()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return data or None


def _flac_from_pcm(pcm: bytes, rate: int = RATE) -> bytes | None:
    if len(pcm) < rate:  # under half a second
        return None
    return _encode_flac(
        ["-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0"],
        pcm,
    )


def _data_url(payload: bytes, mime: str = "audio/flac") -> str:
    return f"data:{mime};base64," + base64.b64encode(payload).decode("ascii")


class _LiveTail:
    """Last few seconds of a live stream's sound, as 16 kHz mono PCM."""

    def __init__(self, source: str, seconds: float = CLIP_S):
        ffmpeg = _ffmpeg()
        if not ffmpeg:
            raise RuntimeError("ffmpeg is not on PATH")
        cmd = [ffmpeg, "-nostdin", "-v", "error"]
        if is_rtsp_url(source):
            cmd += ["-rtsp_transport", "tcp"]
        cmd += [
            "-i",
            source,
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(RATE),
            "-f",
            "s16le",
            "pipe:1",
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._cap = int(RATE * 2 * seconds)
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read, name="source-audio", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        stream = self._proc.stdout
        if stream is None:
            return
        while not self._stop.is_set():
            chunk = stream.read(RATE * 2 // 5)
            if not chunk:
                break
            with self._lock:
                self._buf.extend(chunk)
                extra = len(self._buf) - self._cap
                if extra > 0:
                    del self._buf[:extra]

    def clip(self) -> bytes | None:
        with self._lock:
            pcm = bytes(self._buf)
        return _flac_from_pcm(pcm)

    def close(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


class SourceAudio:
    """Clip of the source's own sound, ready to send beside a frame."""

    def __init__(self, source: str | None):
        self.source = source if isinstance(source, str) else ""
        self._media_t = 0.0
        self._mode = "off"
        self._live: _LiveTail | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        if self.source and _ffmpeg() and (is_rtsp_url(self.source) or is_mjpeg_url(self.source) or is_video_file(self.source)):
            self._thread = threading.Thread(target=self._open, name="source-audio-open", daemon=True)
            self._thread.start()
        elif self.source and not _ffmpeg():
            print("nemotron audio: ffmpeg is not on PATH, so only the picture is sent", flush=True)

    def note_time(self, seconds: float) -> None:
        """File playback position, in seconds, of the frame now on screen."""
        self._media_t = max(0.0, float(seconds))

    def _open(self) -> None:
        if not has_audio_track(self.source):
            print(
                "nemotron audio: this source has no sound track, so only the picture is sent",
                flush=True,
            )
            return
        if is_video_file(self.source):
            with self._lock:
                self._mode = "file"
            print("nemotron audio: the file's sound track is sent with the frame", flush=True)
            return
        try:
            live = _LiveTail(self.source)
        except (OSError, RuntimeError) as exc:
            print(f"nemotron audio: could not open the live sound track ({exc})", flush=True)
            return
        with self._lock:
            self._live = live
            self._mode = "live"
        print("nemotron audio: the live sound track is sent with the frame", flush=True)

    def data_url(self) -> str | None:
        """FLAC data URL for the recent sound, or None when this source has none."""
        with self._lock:
            mode = self._mode
            live = self._live
            end = self._media_t
        if mode == "live" and live is not None:
            payload = live.clip()
        elif mode == "file":
            payload = _file_clip(self.source, end)
        else:
            return None
        if not payload:
            return None
        return _data_url(payload)

    def close(self) -> None:
        with self._lock:
            live = self._live
            self._live = None
            self._mode = "off"
        if live is not None:
            live.close()


def _file_clip(path: str, end_s: float, seconds: float = CLIP_S) -> bytes | None:
    ffmpeg = _ffmpeg()
    if not ffmpeg or not Path(path).is_file():
        return None
    end = max(0.0, end_s)
    start = max(0.0, end - seconds)
    length = min(seconds, max(0.3, end - start))
    return _encode_flac(
        [
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{length:.3f}",
            "-i",
            path,
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(RATE),
        ]
    )
