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

import base64
import hmac
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse

MTX_HOST = os.environ.get("MTX_HOST", "mediamtx")
RTSP_PORT = os.environ.get("RTSP_PORT", "8554")
LAN_IP = os.environ.get("MEDIAMTX_LAN_IP", "10.128.93.13")
API_PORT = int(os.environ.get("API_PORT", "8090"))
API_HOST = os.environ.get("API_HOST", "127.0.0.1")
API_USER = os.environ.get("MTX_API_USER", "")
API_PASSWORD = os.environ.get("MTX_API_PASSWORD", "")
DATA_PATH = os.environ.get("DATA_PATH", "/data/cameras.json")
MTX_PUBLISH_USER = os.environ.get("MTX_PUBLISH_USER", "publisher")
MTX_PUBLISH_PASSWORD = os.environ.get("MTX_PUBLISH_PASSWORD", "")
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
    """Print a message immediately so service logs stay up to date."""
    print(msg, flush=True)


def host_of(raw: str) -> str:
    """Get the host name or IP address from a camera address or URL."""
    text = raw.strip()
    if "://" not in text:
        text = "http://" + text
    host = urlparse(text).hostname
    if not host:
        raise ValueError("camera address is missing a host")
    return host


_USERINFO = re.compile(r"(://[^/\s:@]+):([^@\s/]+)@")
_SENSITIVE_QUERY = re.compile(
    r"(?i)([?&](?:access_key|access_token|api_key|auth|key|pass|password|token)=)[^&\s'\"<>]*"
)


def redact(raw: str) -> str:
    """Hide URL passwords before addresses or ffmpeg output are logged."""
    if "|" in raw:
        return "|".join(redact(part) for part in raw.split("|"))
    raw = _USERINFO.sub(r"\1:***@", raw)
    raw = _SENSITIVE_QUERY.sub(r"\1***", raw)
    if "://" not in raw or " " in raw:
        return raw
    parsed = urlparse(raw)
    sensitive_query_keys = {
        "access_key", "access_token", "api_key", "auth", "key", "pass",
        "password", "token",
    }
    query = urlencode(
        [
            (key, "***" if key.lower() in sensitive_query_keys else value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    if not parsed.username and not parsed.password:
        return urlunparse(parsed._replace(query=query))
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    user = quote(parsed.username or "", safe="")
    return urlunparse(parsed._replace(netloc=f"{user}:***@{host}{port}", query=query))


def with_auth(url: str, user: str | None, password: str | None) -> str:
    """Add login details to a URL only when it does not already have them."""
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
    """Check whether a URL looks like an MJPEG camera stream."""
    parsed = urlparse(url if "://" in url else "http://" + url)
    path = (parsed.path or "").lower().rstrip("/")
    if (parsed.port == 81 or parsed.netloc.endswith(":81")) and path.endswith("/stream"):
        return True
    return path.endswith((".mjpg", ".mjpeg")) or path.endswith("/mjpeg")


def host_literal(ip: str) -> str:
    """Format an IPv6 address so it can safely be used inside a URL."""
    if ":" in ip and not ip.startswith("["):
        return f"[{ip}]"
    return ip


def probe_http(url: str, timeout: float = 2.5) -> bool:
    """Check whether an HTTP camera URL returns stream data."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    request_url = urlunparse(parsed._replace(netloc=f"{host}{port}"))
    handlers: list = []
    if parsed.username:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(
            None,
            request_url,
            unquote(parsed.username),
            unquote(parsed.password or ""),
        )
        handlers.append(urllib.request.HTTPDigestAuthHandler(mgr))
        handlers.append(urllib.request.HTTPBasicAuthHandler(mgr))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(request_url, method="GET")
    try:
        with opener.open(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) >= 400:
                return False
            ctype = (resp.headers.get("content-type") or "").lower()
            if "text/html" in ctype or "application/json" in ctype:
                return False
            return bool(resp.read(1))
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return False


def probe_rtsp(url: str, timeout: float = 2.5) -> bool:
    """Check whether an RTSP URL has a video stream using ffprobe."""
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
    """Choose the RTSP or HTTP check that matches the URL scheme."""
    if url.lower().startswith(("rtsp://", "rtsps://")):
        return probe_rtsp(url, timeout)
    return probe_http(url, timeout)


def urls_that_answer(urls: list[str], probe, budget: float = 4.0, workers: int = 8) -> set[str]:
    """Probe URLs concurrently and return the ones that respond in time."""
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
    """Build a stream description for an ESP Sense camera's video and audio."""
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
    """Build a stream description for a single camera URL."""
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
    sense_audio = with_auth(f"http://{host}/audio", user, password)
    sense_video = with_auth(f"http://{host}:81/stream", user, password)
    rtsp_urls = [
        with_auth(f"rtsp://{host}:554{path}", user, password) for path in RTSP_PATHS
    ]
    # Picture ports first. A closed RTSP port that drops packets would otherwise
    # occupy every worker until the deadline, and the ESP streams would never be checked.
    found = urls_that_answer([sense_audio, sense_video], probe, budget=3.0, workers=2)
    if sense_audio in found and sense_video in found:
        return sense_spec(host, video=sense_video, audio=sense_audio)
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
    """Turn a camera address or URL into a normalized stream description."""
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
        raise ValueError("camera URL is missing a host")
    path = parsed.path or ""
    if scheme in ("rtsp", "rtsps"):
        return stream_spec(text, host, "rtsp")
    if scheme in ("http", "https"):
        if path.rstrip("/") == "/audio":
            sense_user = unquote(parsed.username) if parsed.username else None
            sense_password = unquote(parsed.password or "") if parsed.username else None
            video = with_auth(
                f"http://{host_literal(host)}:81/stream", sense_user, sense_password
            )
            return sense_spec(host, video=video, audio=text)
        if looks_mjpeg(text):
            return stream_spec(text, host, "mjpeg")
        return stream_spec(text, host, "http")
    raise ValueError(f"unsupported camera URL scheme: {scheme or '(missing)'}")


def parse_item(item, probe=None) -> dict:
    """Normalize one camera entry from the API request, including credentials."""
    if isinstance(item, str):
        return classify(item, probe)
    if not isinstance(item, dict):
        raise ValueError("each camera must be a string or an object")
    user = item.get("user") or item.get("username")
    password = item.get("password")
    kind = str(item.get("kind") or item.get("type") or "").lower()
    if kind == "sense":
        if item.get("video") and item.get("audio"):
            video = with_auth(str(item["video"]), user, password)
            audio = with_auth(str(item["audio"]), user, password)
            return sense_spec(str(item.get("ip") or host_of(video)), video, audio)
        if item.get("video") or item.get("audio"):
            raise ValueError("a Sense camera object must provide both video and audio URLs")
        raw_sense = item.get("url") or item.get("source") or item.get("ip")
        if isinstance(raw_sense, str) and raw_sense.strip():
            raw_sense = raw_sense.strip()
            if "://" not in raw_sense:
                return sense_spec(host_of(raw_sense))
            parsed_sense = urlparse(raw_sense)
            if parsed_sense.path.rstrip("/") == "/audio":
                sense_user = user or (unquote(parsed_sense.username) if parsed_sense.username else None)
                sense_password = password
                if sense_password is None and parsed_sense.username:
                    sense_password = unquote(parsed_sense.password or "")
                audio = with_auth(raw_sense, sense_user, sense_password)
                host = host_literal(host_of(audio))
                video = with_auth(f"http://{host}:81/stream", sense_user, sense_password)
                return sense_spec(host, video=video, audio=audio)
        raise ValueError("a Sense camera needs an IP, an /audio URL, or both video and audio URLs")
    if (
        kind in ("mjpeg", "rtsp", "http")
        and not user
        and not password
        and item.get("url")
    ):
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
    """Extract camera entries from the supported API request formats."""
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
    """Give each camera a stable, numbered MediaMTX path name."""
    return "cam_sense" if index == 1 else f"cam_sense{index}"


def runtime_spec(spec: dict, name: str) -> dict:
    """Add publisher details to an internal spec that may contain credentials."""
    """Add the assigned path and client-facing RTSP URL to a stream description."""
    out = {
        "source": spec["source"],
        "ip": spec["ip"],
        "kind": spec["kind"],
        "path": name,
        "rtsp": f"rtsp://{LAN_IP}:8554/{name}",
    }
    if spec["kind"] == "sense":
        out["video"] = spec["video"]
        out["audio"] = spec["audio"]
    else:
        out["url"] = spec["url"]
    return out


def validate_source_urls(spec: dict) -> None:
    """Reject malformed source URLs and invalid ports before replacing streams."""
    urls = (spec["video"], spec["audio"]) if spec["kind"] == "sense" else (spec["url"],)
    for url in urls:
        try:
            parsed = urlparse(url)
            if not parsed.scheme or not parsed.hostname:
                raise ValueError("source URL must include a scheme and host")
            scheme = parsed.scheme.lower()
            if spec["kind"] == "rtsp" and scheme not in ("rtsp", "rtsps"):
                raise ValueError("RTSP camera URLs must use rtsp:// or rtsps://")
            if spec["kind"] in ("http", "mjpeg", "sense") and scheme not in ("http", "https"):
                raise ValueError("HTTP camera URLs must use http:// or https://")
            port = parsed.port
            if port is not None and not 1 <= port <= 65535:
                raise ValueError("source URL port must be between 1 and 65535")
        except ValueError as exc:
            raise ValueError(f"invalid camera source URL: {exc}") from exc


def client_spec(spec: dict) -> dict:
    """Return camera status without source URLs or embedded credentials."""
    return {
        "ip": spec["ip"],
        "kind": spec["kind"],
        "path": spec["path"],
        "rtsp": spec["rtsp"],
    }


def publish_url(path: str) -> str:
    """Build the authenticated RTSP URL used by this service to publish a stream."""
    if not MTX_PUBLISH_USER or not MTX_PUBLISH_PASSWORD:
        raise RuntimeError("MTX_PUBLISH_USER and MTX_PUBLISH_PASSWORD must be set")
    credentials = (
        f"{quote(MTX_PUBLISH_USER, safe='')}:{quote(MTX_PUBLISH_PASSWORD, safe='')}@"
    )
    return f"rtsp://{credentials}{MTX_HOST}:{RTSP_PORT}/{path}"


def ffmpeg_sense(spec: dict) -> list[str]:
    """ESP Sense: MJPEG :81/stream plus PCM /audio → H.264 + AAC."""
    out = publish_url(spec["path"])
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
    out = publish_url(spec["path"])
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
        "-dn",
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
        "-pix_fmt", "yuv420p", "-bf", "0", "-g", "30", "-keyint_min", "30",
        "-tune", "zerolatency", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-f", "rtsp", "-rtsp_transport", "tcp", out,
    ]
    return cmd


def ffmpeg_cmd(spec: dict) -> list[str]:
    """Choose the ffmpeg command that matches the camera type."""
    if spec.get("kind") == "sense":
        return ffmpeg_sense(spec)
    return ffmpeg_stream(spec)


class Supervisor:
    """Manage camera stream processes and their saved configuration."""

    def __init__(self) -> None:
        """Initialize process tracking and synchronization state."""
        self._lock = threading.Lock()
        self._generation = 0
        self._procs: dict[str, subprocess.Popen] = {}
        self._specs: list[dict] = []

    def snapshot(self) -> list[dict]:
        """Return a safe copy of the currently configured cameras."""
        with self._lock:
            return [client_spec(spec) for spec in self._specs]

    def replace(self, items: list) -> tuple[list[dict], list[str]]:
        """Replace the camera list, save it, and start its stream processes."""
        specs, skipped = self._build(items)
        with self._lock:
            self._generation += 1
            gen = self._generation
            procs = self._take_procs_locked()
        self._wait_procs(procs)
        with self._lock:
            if gen != self._generation:
                return [client_spec(spec) for spec in self._specs], []
            self._specs = specs
            self._save_locked()
            for spec in specs:
                self._start_locked(spec, gen)
        log(
            "cameras: "
            + (", ".join(f"{redact(s['source'])}->{s['path']}" for s in specs) or "(none)")
        )
        return [client_spec(spec) for spec in specs], skipped

    def restore(self) -> None:
        """Load the saved camera list and restart its streams, if any."""
        items = self._load()
        if not items:
            return
        log(f"restore {len(items)} camera(s)")
        try:
            self.replace(items)
        except ValueError as exc:
            log(f"restore skipped: {exc}")

    def stop(self) -> None:
        """Stop all active stream processes."""
        with self._lock:
            self._generation += 1
            procs = self._take_procs_locked()
        self._wait_procs(procs)

    def _build(self, items: list, probe=None) -> tuple[list[dict], list[str]]:
        """Validate camera entries, remove duplicates, and assign path names."""
        specs: list[dict] = []
        skipped: list[str] = []
        seen: set[str] = set()
        for item in items:
            try:
                parsed = parse_item(item, probe)
                validate_source_urls(parsed)
            except ValueError as exc:
                skipped.append(str(exc))
                continue
            if parsed["source"] in seen:
                continue
            seen.add(parsed["source"])
            specs.append(runtime_spec(parsed, path_name(len(specs) + 1)))
        for err in skipped:
            log(f"skip {err}")
        if skipped:
            raise ValueError("; ".join(skipped))
        return specs, skipped

    def _start_locked(self, spec: dict, gen: int) -> None:
        """Launch ffmpeg for a camera and start its log and restart watchers."""
        cmd = ffmpeg_cmd(spec)
        log(f"start {spec['path']} {redact(spec['source'])} ({spec['kind']})")
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._procs[spec["source"]] = proc
        threading.Thread(
            target=self._log_stderr, args=(spec["path"], proc), daemon=True
        ).start()
        threading.Thread(
            target=self._watch, args=(spec, gen, proc), daemon=True
        ).start()

    def _log_stderr(self, path: str, proc: subprocess.Popen) -> None:
        """Write a stream process's error output to the service log safely."""
        if proc.stderr is None:
            return
        for line in proc.stderr:
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                log(f"{path} {redact(text)}")

    def _watch(self, spec: dict, gen: int, proc: subprocess.Popen) -> None:
        """Restart a stream process if it exits and the camera is still active."""
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
        """Remove and return all tracked processes while holding the lock."""
        procs = list(self._procs.values())
        self._procs.clear()
        return procs

    def _wait_procs(self, procs: list[subprocess.Popen]) -> None:
        """Stop processes gracefully, then force-stop any that time out."""
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
        """Save the current camera list using an atomic file replacement."""
        parent = os.path.dirname(DATA_PATH)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="cameras-", suffix=".tmp", dir=parent or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"cameras": self._specs}, fh)
            os.replace(tmp, DATA_PATH)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _load(self) -> list:
        """Read the saved camera list, including older supported file formats."""
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
    """Handle requests for camera status and camera-list updates."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        """Send the HTTP server's access log through the service logger."""
        log("api " + (fmt % args))

    def _json(self, code: int, payload: dict) -> None:
        """Send a JSON response with the given HTTP status code."""
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        """Check the Basic credentials required to manage camera streams."""
        if not API_USER or not API_PASSWORD:
            return False
        scheme, _, encoded = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "basic" or not encoded:
            return False
        try:
            supplied = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        expected = f"{API_USER}:{API_PASSWORD}"
        return hmac.compare_digest(supplied, expected)

    def _unauthorized(self) -> None:
        """Ask the client for valid Basic credentials without exposing config."""
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="camera publisher"')
        self.send_header("content-type", "application/json")
        body = json.dumps({"ok": False, "error": "authentication required"}).encode()
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        """Return current camera status for the health and camera-list routes."""
        if not self._authorized():
            self._unauthorized()
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", "/cameras", "/health"):
            cams = SUPER.snapshot()
            self._json(200, {"ok": True, "cameras": cams})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        """Validate a camera-list request and replace the active streams."""
        if not self._authorized():
            self._unauthorized()
            return
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
    """Restore saved cameras and run the HTTP API until the service stops."""
    # Bind first: a duplicate instance must fail before it can start publishers.
    server = ThreadingHTTPServer((API_HOST, API_PORT), Handler)

    def shutdown(*_args) -> None:
        """Stop camera processes and exit when the service gets a stop signal."""
        log("stopping")
        SUPER.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        SUPER.restore()
        log(f"camera API on {API_HOST}:{API_PORT}  mediamtx rtsp://{LAN_IP}:8554")
        server.serve_forever()
    finally:
        server.server_close()
        SUPER.stop()


if __name__ == "__main__":
    main()
