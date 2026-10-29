#!/usr/bin/env python3
"""ESP camera publisher for MediaMTX.

POST /cameras  {"ips": ["10.128.93.15", "10.128.93.24"]}
GET  /cameras
The list is saved and republished after a restart.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

MTX_HOST = os.environ.get("MTX_HOST", "mediamtx")
RTSP_PORT = os.environ.get("RTSP_PORT", "8554")
LAN_IP = os.environ.get("MEDIAMTX_LAN_IP", "10.128.93.13")
API_PORT = int(os.environ.get("API_PORT", "8090"))
DATA_PATH = os.environ.get("DATA_PATH", "/data/cameras.json")
FPS = os.environ.get("SENSE_AV_FPS", "8")
AUDIO_DELAY_S = os.environ.get("SENSE_AV_AUDIO_DELAY_S", "0.300")


def log(msg: str) -> None:
    print(msg, flush=True)


def normalize_ip(raw: str) -> str:
    text = raw.strip()
    if "://" in text:
        text = urlparse(text).netloc or urlparse(text).path
    text = text.split("/")[0]
    if text.startswith("[") and "]" in text:
        text = text[1 : text.index("]")]
    else:
        text = text.split(":")[0]
    if not text:
        raise ValueError(f"empty camera address: {raw!r}")
    return text


def path_name(index: int) -> str:
    return "cam_sense" if index == 1 else f"cam_sense{index}"


def describe(ip: str, name: str) -> dict:
    return {
        "ip": ip,
        "kind": "sense",
        "path": name,
        "video": f"http://{ip}:81/stream",
        "audio": f"http://{ip}/audio",
        "rtsp": f"rtsp://{LAN_IP}:8554/{name}",
        "hls": f"http://{LAN_IP}:8888/{name}/",
    }


def ffmpeg_cmd(spec: dict) -> list[str]:
    """H.264 video + AAC audio into one RTSP path."""
    out = f"rtsp://{MTX_HOST}:{RTSP_PORT}/{spec['path']}"
    video = spec["video"]
    gop = str(int(FPS) * 2)
    filt = (
        f"[0:v]fps={FPS},format=yuv420p,setpts=N/({FPS}*TB)[v];"
        "[1:a]highpass=f=80,lowpass=f=7500,"
        "acompressor=threshold=-28dB:ratio=3:attack=15:release=150:makeup=2,"
        "alimiter=limit=0.89,aresample=16000:async=1000:first_pts=0[a]"
    )
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-fflags", "+genpts+discardcorrupt",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-rw_timeout", "15000000",
        "-probesize", "512k", "-analyzeduration", "500000",
        "-thread_queue_size", "8",
        "-f", "mjpeg", "-framerate", FPS, "-i", video,
        "-itsoffset", AUDIO_DELAY_S,
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-rw_timeout", "15000000",
        "-thread_queue_size", "2048",
        "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", spec["audio"],
        "-filter_complex", filt,
        "-map", "[v]", "-map", "[a]",
        "-fps_mode", "cfr", "-r", FPS,
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
        "-pix_fmt", "yuv420p", "-bf", "0", "-g", gop, "-keyint_min", gop,
        "-tune", "zerolatency", "-crf", "18", "-maxrate", "6000k", "-bufsize", "500k",
        "-x264-params", "scenecut=0:repeat-headers=1:rc-lookahead=0:sync-lookahead=0",
        "-c:a", "aac", "-b:a", "96k", "-ar", "16000", "-ac", "1",
        "-max_interleave_delta", "0", "-muxdelay", "0", "-muxpreload", "0",
        "-f", "rtsp", "-rtsp_transport", "tcp", out,
    ]


class Supervisor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._procs: dict[str, subprocess.Popen] = {}
        self._specs: list[dict] = []

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._specs)

    def replace(self, ips: list[str]) -> list[dict]:
        specs = self._build(ips)
        with self._lock:
            self._generation += 1
            gen = self._generation
            procs = self._take_procs_locked()
        self._wait_procs(procs)
        with self._lock:
            if gen != self._generation:
                return list(self._specs)
            self._specs = specs
            self._save_locked()
            for spec in specs:
                self._start_locked(spec, gen)
        log("cameras: " + ", ".join(f"{s['ip']}->{s['path']}" for s in specs) or "(none)")
        return specs

    def restore(self) -> None:
        ips = self._load()
        if ips:
            log(f"restore {len(ips)} camera(s)")
            self.replace(ips)

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            procs = self._take_procs_locked()
        self._wait_procs(procs)

    def _build(self, ips: list[str]) -> list[dict]:
        specs = []
        seen = set()
        for raw in ips:
            ip = normalize_ip(raw)
            if ip in seen:
                continue
            seen.add(ip)
            specs.append(describe(ip, path_name(len(specs) + 1)))
        return specs

    def _start_locked(self, spec: dict, gen: int) -> None:
        cmd = ffmpeg_cmd(spec)
        log(f"start {spec['path']} {spec['ip']} ({spec['kind']})")
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        self._procs[spec["ip"]] = proc
        threading.Thread(
            target=self._watch, args=(spec, gen, proc), daemon=True
        ).start()

    def _watch(self, spec: dict, gen: int, proc: subprocess.Popen) -> None:
        code = proc.wait()
        log(f"exit {spec['path']} code={code}")
        time.sleep(3)
        with self._lock:
            if gen != self._generation:
                return
            if not any(s["ip"] == spec["ip"] for s in self._specs):
                return
            current = self._procs.get(spec["ip"])
            if current is not None and current is not proc and current.poll() is None:
                return
            self._start_locked(spec, gen)

    def _take_procs_locked(self) -> list[subprocess.Popen]:
        procs = list(self._procs.values())
        self._procs.clear()
        return procs

    def _wait_procs(self, procs: list[subprocess.Popen]) -> None:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    def _save_locked(self) -> None:
        parent = os.path.dirname(DATA_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = DATA_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"ips": [s["ip"] for s in self._specs]}, fh)
        os.replace(tmp, DATA_PATH)

    def _load(self) -> list[str]:
        try:
            with open(DATA_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return []
        ips = data.get("ips") if isinstance(data, dict) else data
        if not isinstance(ips, list):
            return []
        return [str(ip) for ip in ips]


SUPER = Supervisor()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        log("api " + (fmt % args))

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", "/cameras", "/health"):
            cams = SUPER.snapshot()
            self._json(200, {"ok": True, "cameras": cams})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path != "/cameras":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("content-length", "0") or 0)
        except ValueError:
            self._json(400, {"ok": False, "error": "bad content-length"})
            return
        if length < 0 or length > 65536:
            self._json(400, {"ok": False, "error": "body too large"})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "body must be JSON"})
            return
        ips = data.get("ips") if isinstance(data, dict) else data
        if ips is None and isinstance(data, dict):
            ips = data.get("urls")
        if not isinstance(ips, list) or not all(isinstance(x, str) for x in ips):
            self._json(400, {"ok": False, "error": 'send {"ips": ["10.128.93.15"]}'})
            return
        try:
            cams = SUPER.replace(ips)
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        self._json(200, {"ok": True, "cameras": cams})


def main() -> None:
    SUPER.restore()

    def shutdown(*_args) -> None:
        log("stopping")
        SUPER.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server = ThreadingHTTPServer(("0.0.0.0", API_PORT), Handler)
    log(f"camera API on :{API_PORT}  mediamtx rtsp://{LAN_IP}:8554")
    server.serve_forever()


if __name__ == "__main__":
    main()
