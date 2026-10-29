#!/usr/bin/env python3
"""Camera publisher for MediaMTX.

POST /cameras replaces the whole set. Each entry is any camera IP, or a
stream URL (rtsp://, rtsps://, http://, https://).

  {"ips": ["10.128.93.15", "192.168.1.50"]}
  {"cameras": [{"ip": "192.168.1.50", "user": "admin", "password": "secret"}]}

A bare IP is searched for a real stream: common RTSP paths, common HTTP
picture paths, and the ESP Sense mic+picture pair. A full URL is used as
given. The list is saved and republished after a restart.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse, urlunparse

MTX_HOST = os.environ.get("MTX_HOST", "mediamtx")
RTSP_PORT = os.environ.get("RTSP_PORT", "8554")
LAN_IP = os.environ.get("MEDIAMTX_LAN_IP", "10.128.93.13")
API_PORT = int(os.environ.get("API_PORT", "8090"))
DATA_PATH = os.environ.get("DATA_PATH", "/data/cameras.json")
FPS = os.environ.get("SENSE_AV_FPS", "8")
AUDIO_DELAY_S = os.environ.get("SENSE_AV_AUDIO_DELAY_S", "0.300")
KINDS = ("sense", "mjpeg", "rtsp", "http")
# Tried in this order when the caller only has an IP. First hit with video wins
# after the ESP Sense pair (picture on :81 plus /audio).
RTSP_PATHS = (
    "/profile2/media.smp",
    "/profile1/media.smp",
    "/onvif/profile2/media.smp",
    "/onvif/profile1/media.smp",
    "/Streaming/Channels/101",
    "/cam/realmonitor?channel=1&subtype=0",
    "/stream1",
    "/live/ch00_0",
    "/h264Preview_01_main",
    "/axis-media/media.amp",
    "/media/video1",
    "/h264",
    "/live",
    "/",
)
HTTP_STREAMS = (
    "http://{host}:81/stream",
    "http://{host}/mjpg/video.mjpg",
    "http://{host}/video.mjpg",
    "http://{host}/cgi-bin/mjpg/video.cgi",
    "http://{host}/mjpeg",
    "http://{host}/stream",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def host_of(raw: str) -> str:
    text = raw.strip()
    if "://" not in text:
        text = "http://" + text
    host = urlparse(text).hostname
    if not host:
        raise ValueError(f"empty camera address: {raw!r}")
    return host


def redact(raw: str) -> str:
    if "|" in raw:
        return "|".join(redact(part) for part in raw.split("|"))
    if "://" not in raw:
        return raw
    parsed = urlparse(raw)
    if not parsed.username and not parsed.password:
        return raw
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    user = quote(parsed.username or "", safe="")
    return urlunparse(parsed._replace(netloc=f"{user}:***@{host}{port}"))


def with_auth(url: str, user: str | None, password: str | None) -> str:
    if not user or "://" not in url:
        return url
    parsed = urlparse(url)
    if parsed.username:
        return url
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{quote(user, safe='')}:{quote(password or '', safe='')}@{host}{port}"
    return urlunparse(parsed._replace(netloc=netloc))


def looks_mjpeg(url: str) -> bool:
    parsed = urlparse(url if "://" in url else "http://" + url)
    path = (parsed.path or "").lower().rstrip("/")
    if (parsed.port == 81 or parsed.netloc.endswith(":81")) and path.endswith("/stream"):
        return True
    return path.endswith((".mjpg", ".mjpeg")) or path.endswith("/mjpeg")


def host_literal(ip: str) -> str:
    if ":" in ip and not ip.startswith("["):
        return f"[{ip}]"
    return ip


def probe_http(url: str, timeout: float = 2.5) -> bool:
    parsed = urlparse(url)
    handlers: list = []
    if parsed.username:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, unquote(parsed.username), unquote(parsed.password or ""))
        handlers.append(urllib.request.HTTPDigestAuthHandler(mgr))
        handlers.append(urllib.request.HTTPBasicAuthHandler(mgr))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, method="GET")
    try:
        with opener.open(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) >= 400:
                return False
            ctype = (resp.headers.get("content-type") or "").lower()
            if "text/html" in ctype or "application/json" in ctype:
                return False
            resp.read(1)
            return True
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return False


def probe_rtsp(url: str, timeout: float = 2.5) -> bool:
    micros = str(int(timeout * 1_000_000))
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-rtsp_transport", "tcp",
                "-timeout", micros,
                "-select_streams", "v",
                "-show_entries", "stream=codec_type",
                "-of", "csv=p=0",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and "video" in proc.stdout


def probe_url(url: str, timeout: float = 2.5) -> bool:
    if url.lower().startswith(("rtsp://", "rtsps://")):
        return probe_rtsp(url, timeout)
    return probe_http(url, timeout)


def urls_that_answer(urls: list[str], probe, budget: float = 4.0, workers: int = 8) -> set[str]:
    ok: set[str] = set()
    if not urls:
        return ok
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(urls))))
    futs = {pool.submit(probe, url): url for url in urls}
    try:
        for fut in as_completed(futs, timeout=budget):
            try:
                if fut.result():
                    ok.add(futs[fut])
            except Exception:
                continue
    except TimeoutError:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return ok


def sense_spec(ip: str, video: str | None = None, audio: str | None = None) -> dict:
    video = video or f"http://{ip}:81/stream"
    audio = audio or f"http://{ip}/audio"
    default = video == f"http://{ip}:81/stream" and audio == f"http://{ip}/audio"
    return {
        "source": ip if default else f"{video}|{audio}",
        "kind": "sense",
        "ip": ip,
        "video": video,
        "audio": audio,
        "url": None,
    }


def stream_spec(url: str, host: str, kind: str) -> dict:
    return {
        "source": url,
        "kind": kind,
        "ip": host,
        "video": None,
        "audio": None,
        "url": url,
    }


def classify_host(ip: str, probe=None, user: str | None = None, password: str | None = None) -> dict:
    """Find a stream for any camera that is identified only by its address."""
    probe = probe or probe_url
    host = host_literal(ip)
    sense_audio = f"http://{host}/audio"
    sense_video = f"http://{host}:81/stream"
    rtsp_urls = [
        with_auth(f"rtsp://{host}:554{path}", user, password) for path in RTSP_PATHS
    ]
    # Picture ports first. A closed RTSP port that drops packets would otherwise
    # occupy every worker until the deadline, and the ESP streams would never be checked.
    found = urls_that_answer([sense_audio, sense_video], probe, budget=3.0, workers=2)
    if sense_audio in found and sense_video in found:
        return sense_spec(host)
    if sense_video in found:
        return stream_spec(sense_video, ip, "mjpeg")
    http_urls: list[str] = []
    for tmpl in HTTP_STREAMS:
        url = tmpl.format(host=host)
        if url == sense_video:
            continue
        if user:
            url = with_auth(url, user, password)
        if url not in http_urls:
            http_urls.append(url)
    found = urls_that_answer(http_urls, probe, budget=4.0, workers=len(http_urls) or 1)
    for url in http_urls:
        if url in found:
            return stream_spec(url, ip, "mjpeg" if looks_mjpeg(url) else "http")
    found = urls_that_answer(rtsp_urls, probe, budget=5.0, workers=len(rtsp_urls) or 1)
    for url in rtsp_urls:
        if url in found:
            return stream_spec(url, ip, "rtsp")
    hint = f"rtsp://{ip}:554/profile2/media.smp"
    extra = " Check the username and password." if user or password else ""
    raise ValueError(f"no video stream found at {ip}. Post the camera URL, for example {hint}.{extra}")


def classify(raw: str, probe=None) -> dict:
    probe = probe or probe_url
    text = raw.strip()
    if not text:
        raise ValueError("empty camera address")
    if "://" not in text:
        return classify_host(text, probe)
    parsed = urlparse(text)
    scheme = (parsed.scheme or "").lower()
    host = parsed.hostname
    if not host:
        raise ValueError(f"empty camera address: {raw!r}")
    path = parsed.path or ""
    if scheme in ("rtsp", "rtsps"):
        return stream_spec(text, host, "rtsp")
    if scheme in ("http", "https"):
        if path in ("", "/"):
            return classify_host(host, probe, parsed.username, parsed.password)
        if path.rstrip("/") == "/audio":
            return sense_spec(host)
        if looks_mjpeg(text):
            return stream_spec(text, host, "mjpeg")
        return stream_spec(text, host, "http")
    raise ValueError(f"unsupported camera URL: {text}")


def parse_item(item, probe=None) -> dict:
    if isinstance(item, str):
        return classify(item, probe)
    if not isinstance(item, dict):
        raise ValueError("each camera must be a string or an object")
    user = item.get("user") or item.get("username")
    password = item.get("password")
    kind = str(item.get("kind") or item.get("type") or "").lower()
    if (
        kind in KINDS
        and not user
        and not password
        and (item.get("url") or (item.get("video") and item.get("audio")))
    ):
        if kind == "sense":
            video = str(item["video"])
            return sense_spec(str(item.get("ip") or host_of(video)), video, str(item["audio"]))
        url = str(item["url"])
        return stream_spec(url, str(item.get("ip") or host_of(url)), kind)
    if item.get("video"):
        video = with_auth(str(item["video"]), user, password)
        ip = str(item.get("ip") or host_of(video))
        if item.get("audio"):
            return sense_spec(ip, video, with_auth(str(item["audio"]), user, password))
        if video.lower().startswith(("rtsp://", "rtsps://")):
            return stream_spec(video, ip, "rtsp")
        return stream_spec(video, ip, "mjpeg" if looks_mjpeg(video) else "http")
    raw = item.get("url") or item.get("source") or item.get("ip")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("camera needs an ip or a url")
    raw = raw.strip()
    if kind == "sense":
        return sense_spec(host_of(raw))
    if kind == "mjpeg":
        url = raw if "://" in raw else f"http://{raw}:81/stream"
        return stream_spec(with_auth(url, user, password), host_of(url), "mjpeg")
    if kind == "rtsp":
        url = raw if raw.lower().startswith(("rtsp://", "rtsps://")) else f"rtsp://{raw}"
        return stream_spec(with_auth(url, user, password), host_of(url), "rtsp")
    if "://" in raw:
        return classify(with_auth(raw, user, password), probe)
    return classify_host(host_of(raw), probe or probe_url, user, password)


def camera_items(data) -> list:
    """Accept a list of IPs, URLs, and {ip, user, password} objects."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("ips", "urls", "cameras", "sources"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        if any(data.get(key) for key in ("ip", "url", "video", "source")):
            return [data]
    raise ValueError(
        'send {"ips": ["10.128.93.15"]} or '
        '{"cameras": [{"ip": "192.168.1.50", "user": "admin", "password": "secret"}]}'
    )


def path_name(index: int) -> str:
    return "cam_sense" if index == 1 else f"cam_sense{index}"


def public_spec(spec: dict, name: str) -> dict:
    out = {
        "source": spec["source"],
        "ip": spec["ip"],
        "kind": spec["kind"],
        "path": name,
        "rtsp": f"rtsp://{LAN_IP}:8554/{name}",
        "hls": f"http://{LAN_IP}:8888/{name}/",
    }
    if spec["kind"] == "sense":
        out["video"] = spec["video"]
        out["audio"] = spec["audio"]
    else:
        out["url"] = spec["url"]
    return out


def ffmpeg_sense(spec: dict) -> list[str]:
    """ESP Sense: MJPEG :81/stream plus PCM /audio → H.264 + AAC."""
    out = f"rtsp://{MTX_HOST}:{RTSP_PORT}/{spec['path']}"
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
        "-f", "mjpeg", "-framerate", FPS, "-i", spec["video"],
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


def ffmpeg_stream(spec: dict) -> list[str]:
    """Any RTSP or HTTP camera → H.264, plus AAC when the source has audio."""
    out = f"rtsp://{MTX_HOST}:{RTSP_PORT}/{spec['path']}"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-fflags", "+genpts+discardcorrupt",
    ]
    if spec["kind"] == "rtsp":
        cmd += ["-rtsp_transport", "tcp", "-timeout", "15000000"]
    else:
        cmd += [
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-rw_timeout", "15000000",
        ]
        if spec["kind"] == "mjpeg":
            cmd += ["-f", "mjpeg"]
    cmd += [
        "-i", spec["url"],
        "-vf", "format=yuv420p",
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
        "-pix_fmt", "yuv420p", "-bf", "0", "-g", "30", "-keyint_min", "30",
        "-tune", "zerolatency", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-f", "rtsp", "-rtsp_transport", "tcp", out,
    ]
    return cmd


def ffmpeg_cmd(spec: dict) -> list[str]:
    if spec.get("kind") == "sense":
        return ffmpeg_sense(spec)
    return ffmpeg_stream(spec)


class Supervisor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._procs: dict[str, subprocess.Popen] = {}
        self._specs: list[dict] = []

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._specs)

    def replace(self, items: list) -> tuple[list[dict], list[str]]:
        specs, skipped = self._build(items)
        with self._lock:
            self._generation += 1
            gen = self._generation
            procs = self._take_procs_locked()
        self._wait_procs(procs)
        with self._lock:
            if gen != self._generation:
                return list(self._specs), []
            self._specs = specs
            self._save_locked()
            for spec in specs:
                self._start_locked(spec, gen)
        log(
            "cameras: "
            + (", ".join(f"{redact(s['source'])}->{s['path']}" for s in specs) or "(none)")
        )
        return specs, skipped

    def restore(self) -> None:
        items = self._load()
        if not items:
            return
        log(f"restore {len(items)} camera(s)")
        try:
            self.replace(items)
        except ValueError as exc:
            log(f"restore skipped: {exc}")

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            procs = self._take_procs_locked()
        self._wait_procs(procs)

    def _build(self, items: list, probe=None) -> tuple[list[dict], list[str]]:
        specs: list[dict] = []
        skipped: list[str] = []
        seen: set[str] = set()
        for item in items:
            try:
                parsed = parse_item(item, probe)
            except ValueError as exc:
                skipped.append(str(exc))
                continue
            if parsed["source"] in seen:
                continue
            seen.add(parsed["source"])
            specs.append(public_spec(parsed, path_name(len(specs) + 1)))
        for err in skipped:
            log(f"skip {err}")
        if skipped and not specs:
            raise ValueError("; ".join(skipped))
        return specs, skipped

    def _start_locked(self, spec: dict, gen: int) -> None:
        cmd = ffmpeg_cmd(spec)
        log(f"start {spec['path']} {redact(spec['source'])} ({spec['kind']})")
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        self._procs[spec["source"]] = proc
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
            if not any(s["source"] == spec["source"] for s in self._specs):
                return
            current = self._procs.get(spec["source"])
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
            json.dump({"cameras": self._specs}, fh)
        os.replace(tmp, DATA_PATH)

    def _load(self) -> list:
        try:
            with open(DATA_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return []
        if isinstance(data, dict):
            cams = data.get("cameras")
            if isinstance(cams, list) and cams:
                return cams
            ips = data.get("ips") or data.get("sources") or data.get("urls")
            if isinstance(ips, list):
                return [str(ip) for ip in ips]
            return []
        if isinstance(data, list):
            return data
        return []


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
        if length < 0 or length > 262144:
            self._json(400, {"ok": False, "error": "body too large"})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "body must be JSON"})
            return
        try:
            items = camera_items(data)
            cams, skipped = SUPER.replace(items)
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        payload = {"ok": True, "cameras": cams}
        if skipped:
            payload["skipped"] = skipped
        self._json(200, payload)


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
