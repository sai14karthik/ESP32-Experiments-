#!/usr/bin/env python3
"""Record a MediaMTX (or any) RTSP stream to a file with video + audio.

Lab examples (stream must already be publishing via mediamtx_run):

  ./scripts/record_rtsp.py rtsp://127.0.0.1:8554/cam_sense
  ./scripts/record_rtsp.py rtsp://10.128.93.13:8554/cam_sense -o clips/sense-01.mp4 -t 120
  ./scripts/record_rtsp.py rtsp://127.0.0.1:8554/cam_xiao --no-audio   # video-only path

Thin wrapper around ffmpeg (stdlib only). Default: stream-copy video and audio
(no re-encode). Ctrl-C / SIGTERM sends one SIGINT so ffmpeg finishes a playable file.

Exit 0 when the recording completed as asked; 1 if the stream died mid-way
(partial file is kept) or nothing was written.

Adapted from teammate record_rtsp.py — audio kept (their sample used -an).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

STOP_GRACE_S = 15.0
MOV_SUFFIXES = (".mp4", ".m4v", ".mov")
FFMPEG_SIGNALLED = 255


def mask_url(url: str) -> str:
    """Hide user:password@ in URLs for printing."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc="***@" + parts.netloc.rpartition("@")[2]))


def ffmpeg_command(
    ffmpeg: str,
    url: str,
    output: str,
    duration: Optional[float],
    reencode: bool,
    with_audio: bool,
) -> list[str]:
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning", "-stats"]
    if url.lower().startswith(("rtsp://", "rtsps://")):
        # TCP: UDP through NAT/Wi‑Fi often loses packets.
        cmd += ["-rtsp_transport", "tcp"]
    cmd += ["-i", url]
    if duration is not None:
        cmd += ["-t", f"{duration:g}"]

    cmd += ["-map", "0:v:0"]
    if with_audio:
        # Optional map: video-only paths (cam_xiao) still record if no audio track.
        cmd += ["-map", "0:a:0?"]
    else:
        cmd += ["-an"]

    if reencode:
        cmd += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
        ]
        if with_audio:
            cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-c:v", "copy"]
        if with_audio:
            cmd += ["-c:a", "copy"]

    if output.lower().endswith(MOV_SUFFIXES):
        cmd += ["-movflags", "+faststart"]
    return cmd + ["-y", output]


def probe(ffprobe: str, path: str) -> Optional[dict[str, Any]]:
    """Duration + first video/audio stream summary, or None if unreadable."""
    r = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-of",
            "json",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,pix_fmt,width,height,avg_frame_rate,channels,sample_rate",
            path,
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError):
        return None
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {"duration": duration, "video": video, "audio": audio}


def describe(info: dict[str, Any], size: int) -> str:
    parts = [f'{info["duration"]:.1f} s']
    v = info.get("video") or {}
    if v.get("codec_name"):
        parts.append(
            f'{v["codec_name"]} {v.get("width")}x{v.get("height")} {v.get("pix_fmt", "")}'.rstrip()
        )
    num, _, den = str(v.get("avg_frame_rate", "")).partition("/")
    if num.isdigit() and den.isdigit() and int(den):
        parts.append(f"{int(num) / int(den):.3g} fps")
    a = info.get("audio")
    if a and a.get("codec_name"):
        ch = a.get("channels")
        sr = a.get("sample_rate")
        bits = f"{a['codec_name']}"
        if sr:
            bits += f" {sr} Hz"
        if ch:
            bits += f" {ch}ch"
        parts.append(bits)
    else:
        parts.append("no audio")
    parts.append(f"{size / 1e6:.1f} MB")
    return ", ".join(parts)


def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Record an RTSP stream to an MP4 (video + audio by default)."
    )
    ap.add_argument(
        "url",
        help="stream URL, e.g. rtsp://127.0.0.1:8554/cam_sense",
    )
    ap.add_argument(
        "-o",
        "--output",
        help="output file (default: recordings/rtsp-<date>-<time>.mp4)",
    )
    ap.add_argument(
        "-t",
        "--duration",
        type=float,
        help="seconds to record (default: until Ctrl-C)",
    )
    ap.add_argument(
        "-y",
        "--overwrite",
        action="store_true",
        help="replace the output file if it exists",
    )
    ap.add_argument(
        "--reencode",
        action="store_true",
        help="re-encode H.264 (+ AAC if audio) instead of stream-copy",
    )
    ap.add_argument(
        "--no-audio",
        action="store_true",
        help="drop audio (video-only paths like cam_xiao)",
    )
    ap.add_argument(
        "--ffmpeg",
        default="ffmpeg",
        help="ffmpeg binary (default: on PATH)",
    )
    args = ap.parse_args(argv)
    if args.duration is not None and args.duration <= 0:
        ap.error("--duration must be positive")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    ffmpeg = shutil.which(args.ffmpeg)
    if ffmpeg is None:
        print(
            f"record_rtsp: {args.ffmpeg} not found. Install ffmpeg or pass --ffmpeg /path/to/ffmpeg.",
            file=sys.stderr,
        )
        return 1
    ffprobe = shutil.which(os.path.join(os.path.dirname(ffmpeg), "ffprobe")) or shutil.which(
        "ffprobe"
    )

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    default_dir = os.path.join(root, "recordings")
    output = os.path.abspath(
        os.path.expanduser(
            args.output
            or os.path.join(
                default_dir, datetime.now().strftime("rtsp-%Y%m%d-%H%M%S.mp4")
            )
        )
    )
    if os.path.exists(output) and not args.overwrite:
        print(f"record_rtsp: {output} exists; pass -y to replace it.", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    before = os.stat(output).st_mtime_ns if os.path.exists(output) else None

    with_audio = not args.no_audio
    cmd = ffmpeg_command(
        ffmpeg, args.url, output, args.duration, args.reencode, with_audio
    )
    until = f"for {args.duration:g} s" if args.duration is not None else "until Ctrl-C"
    audio_note = "video+audio" if with_audio else "video-only"
    print(f"Recording {mask_url(args.url)} → {output} ({audio_note}, {until})", flush=True)
    print(
        "  "
        + shlex.join(mask_url(c) if c == args.url else c for c in cmd),
        flush=True,
    )

    proc = subprocess.Popen(cmd, start_new_session=True)
    stop_at: list[float] = []

    def request_stop(signum: int, frame: Any) -> None:
        if not stop_at:
            stop_at.append(time.monotonic())
            print(
                "\nStopping; ffmpeg is finishing the file…",
                file=sys.stderr,
                flush=True,
            )
            proc.send_signal(signal.SIGINT)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, request_stop)

    rc: Optional[int] = None
    while rc is None:
        try:
            rc = proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            if stop_at and time.monotonic() - stop_at[0] > STOP_GRACE_S:
                print(
                    f"ffmpeg did not finish within {STOP_GRACE_S:g} s; killing it.",
                    file=sys.stderr,
                )
                proc.kill()

    written = os.path.exists(output) and os.stat(output).st_mtime_ns != before
    if not written:
        print(
            f"No recording: ffmpeg exited with {rc} before writing any media (see message above).",
            file=sys.stderr,
        )
        return 1
    size = os.path.getsize(output)
    info = probe(ffprobe, output) if ffprobe and size else None
    if size == 0 or (ffprobe and (info is None or info["duration"] <= 0)):
        os.remove(output)
        print(
            f"No recording: ffmpeg exited with {rc} and {output} was unreadable (removed).",
            file=sys.stderr,
        )
        return 1

    if with_audio and info and not info.get("audio"):
        print(
            "warning: file has no audio track (source may be video-only; use --no-audio to silence this).",
            file=sys.stderr,
        )

    summary = describe(info, size) if info else f"{size / 1e6:.1f} MB"
    if rc == 0 or (stop_at and rc == FFMPEG_SIGNALLED):
        print(f"Saved {output}: {summary}")
        return 0
    print(
        f"ffmpeg exited with {rc} (stream lost?); kept {output}: {summary}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
