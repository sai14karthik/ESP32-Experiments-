#!/usr/bin/env python3
"""Mobile web live presence (EMPTY / PRESENCE) + ESP TCP client count.

Owns CSI TCP :9055 (same as live/gui) and serves a phone-friendly page on
HTTP :8765 (default).

  ./run_presence.sh web
  python detect_web.py --listen-tcp 9055 --fast --http-port 8765

Phone: http://10.128.93.23:8765 on LabPSK (same LAN as Mini).
"""

from __future__ import annotations

import json
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from detect_live import (
    MultiRxLiveDetector,
    build_live_arg_parser,
    iter_csi_from_tcp,
    load_bundle_and_calibration,
    make_live_detector,
    print_startup_banner,
)

DEFAULT_HTTP_PORT = 8765
DEFAULT_ROOM = "Room 207"
# Keep LIVE through brief TCP reconnect gaps (boards drop old socket then reopen).
DEVICE_LIVE_GRACE_S = 45.0

PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<title>{{ROOM}} · Presence</title>
<style>
  :root {
    --bg: #0f1410;
    --fg: #e8efe6;
    --muted: #8a9a88;
    --empty: #1b3a2f;
    --empty-fg: #7dffa3;
    --presence: #4a1c1c;
    --presence-fg: #ff8a8a;
    --wait: #1a2230;
    --wait-fg: #9eb6d4;
    --live: #3ecf8e;
    --off: #6a7370;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; height: 100%;
    font-family: "SF Pro Text", "Segoe UI", system-ui, sans-serif;
    background: var(--bg); color: var(--fg);
  }
  body {
    display: flex; flex-direction: column;
    min-height: 100dvh;
    padding: max(16px, env(safe-area-inset-top)) 20px
             max(20px, env(safe-area-inset-bottom));
  }
  .top {
    text-align: center;
    padding: 8px 0 4px;
  }
  .top .room {
    margin: 0;
    font-size: clamp(1.4rem, 5vw, 1.85rem);
    font-weight: 700;
    letter-spacing: 0.02em;
  }
  .top .sub {
    margin: 6px 0 0;
    font-size: 0.75rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted);
  }
  #conn {
    font-size: 0.75rem; color: var(--muted);
  }
  #conn.ok { color: var(--empty-fg); }
  #conn.bad { color: var(--presence-fg); }
  .hero {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    gap: 14px;
    min-height: 42vh;
    margin: 12px 0;
  }
  #state {
    width: 100%;
    max-width: 28rem;
    padding: 1.6rem 1rem;
    border-radius: 20px;
    font-size: clamp(2.6rem, 13vw, 4.5rem);
    font-weight: 700;
    letter-spacing: 0.04em;
    transition: background 0.25s, color 0.25s;
    background: var(--wait); color: var(--wait-fg);
  }
  body.empty #state { background: var(--empty); color: var(--empty-fg); }
  body.presence #state { background: var(--presence); color: var(--presence-fg); }
  body.waiting #state { background: var(--wait); color: var(--wait-fg); }
  #line {
    min-height: 1.4em;
    font-size: 0.95rem;
    font-variant-numeric: tabular-nums;
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 100%;
    letter-spacing: 0.01em;
  }
  .devices {
    padding: 12px 14px;
    border-radius: 12px;
    background: rgba(255,255,255,0.04);
  }
  .devices h2 {
    margin: 0 0 10px;
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--muted);
    display: flex;
    justify-content: space-between;
  }
  #device-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 8px;
  }
  #device-list li {
    display: grid;
    grid-template-columns: auto 1fr auto;
    gap: 10px;
    align-items: center;
    font-size: 0.9rem;
    font-variant-numeric: tabular-nums;
  }
  #device-list .dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--off);
  }
  #device-list li.live .dot { background: var(--live); box-shadow: 0 0 8px var(--live); }
  #device-list .ip { font-weight: 600; }
  #device-list .meta {
    color: var(--muted);
    font-size: 0.75rem;
    grid-column: 2 / -1;
  }
  #device-list .badge {
    font-size: 0.7rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--off);
  }
  #device-list li.live .badge { color: var(--live); }
  #device-empty {
    color: var(--muted);
    font-size: 0.85rem;
    display: none;
  }
  #device-empty.show { display: block; }
</style>
</head>
<body class="waiting">
<div class="top">
  <h1 class="room">{{ROOM}}</h1>
  <p class="sub">Presence · <span id="conn">connecting…</span></p>
</div>
<div class="hero">
  <div id="state">WAITING</div>
  <div id="line">—</div>
</div>
<div class="devices">
  <h2>
    <span>Connected to Mini</span>
    <span id="esp">0 live</span>
  </h2>
  <ul id="device-list"></ul>
  <div id="device-empty">No ESP TCP clients on :9055 yet</div>
</div>
<script>
(function () {
  const body = document.body;
  const el = (id) => document.getElementById(id);

  function fmt(n, digits) {
    const x = Number(n);
    if (!Number.isFinite(x)) return "—";
    const s = x.toFixed(digits);
    return (x >= 0 && digits > 0 ? "+" : "") + s;
  }

  function fmtAge(age) {
    if (age == null) return "";
    if (age < 1.5) return "now";
    if (age < 60) return Math.round(age) + "s ago";
    return Math.round(age / 60) + "m ago";
  }

  function renderDevices(s) {
    const list = el("device-list");
    const empty = el("device-empty");
    const devices = s.devices || [];
    const liveN = s.esp_active ?? devices.filter((d) => d.connected).length;
    el("esp").textContent = liveN + " live";
    list.innerHTML = "";
    if (!devices.length) {
      empty.className = "show";
      return;
    }
    empty.className = "";
    for (const d of devices) {
      const li = document.createElement("li");
      li.className = d.connected ? "live" : "off";
      const bits = [];
      if (d.trained) bits.push("trained");
      else if (d.connected) bits.push("new IP");
      if (d.rssi != null) bits.push(d.rssi + " dBm");
      if (d.age_s != null && d.connected) bits.push(fmtAge(d.age_s));
      li.innerHTML =
        '<span class="dot"></span>' +
        '<span class="ip">' + d.ip + '</span>' +
        '<span class="badge">' + (d.connected ? "LIVE" : "OFF") + '</span>' +
        (bits.length ? '<span class="meta">' + bits.join(" · ") + '</span>' : "");
      list.appendChild(li);
    }
  }

  function apply(s) {
    el("conn").textContent = "live";
    el("conn").className = "ok";
    const ready = !!s.ready;
    let label = "WAITING";
    let cls = "waiting";
    if (ready) {
      const st = (s.state || "empty").toLowerCase();
      if (st === "presence" || st === "object") {
        label = "PRESENCE";
        cls = "presence";
      } else {
        label = "EMPTY";
        cls = "empty";
      }
    } else if (s.rx_ready != null) {
      label = "BUFFER";
    }
    body.className = cls;
    el("state").textContent = label;

    const parts = [];
    if (s.score != null && s.threshold != null) {
      parts.push("score " + fmt(s.score, 3) + " / thr " + fmt(s.threshold, 3));
    }
    if (s.rx_present != null) {
      parts.push("rx " + s.rx_present + "/" + (s.rx_total ?? "?"));
    } else if (s.rx_ready != null) {
      parts.push("rx " + s.rx_ready + "/" + (s.rx_need ?? "?"));
    }
    if (s.rssi != null) parts.push(s.rssi + " dBm");
    if (s.seq != null) parts.push("seq " + s.seq);
    el("line").textContent = parts.length ? parts.join("  ·  ") : "—";

    renderDevices(s);
  }

  function fail() {
    el("conn").textContent = "offline";
    el("conn").className = "bad";
  }

  let lastApply = 0;
  let esRef = null;
  let pollTimer = null;
  let mode = "sse";

  function applyWrapped(s) {
    lastApply = Date.now();
    apply(s);
  }

  function stopPoll() {
    if (pollTimer != null) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function startPoll() {
    mode = "poll";
    if (esRef) {
      try { esRef.close(); } catch (_) {}
      esRef = null;
    }
    stopPoll();
    function tick() {
      fetch("/api/status?t=" + Date.now())
        .then((r) => {
          if (!r.ok) throw new Error("status " + r.status);
          return r.json();
        })
        .then(applyWrapped)
        .catch(fail)
        .finally(() => {
          if (mode === "poll") pollTimer = setTimeout(tick, 250);
        });
    }
    tick();
    // Try SSE again after a bit (faster path when the phone allows it).
    setTimeout(() => {
      if (mode === "poll") startSSE();
    }, 8000);
  }

  function startSSE() {
    if (!window.EventSource) {
      startPoll();
      return;
    }
    mode = "sse";
    stopPoll();
    if (esRef) {
      try { esRef.close(); } catch (_) {}
    }
    const es = new EventSource("/api/stream");
    esRef = es;
    lastApply = Date.now();
    es.onmessage = (ev) => {
      try { applyWrapped(JSON.parse(ev.data)); } catch (_) {}
    };
    es.onerror = () => {
      try { es.close(); } catch (_) {}
      esRef = null;
      startPoll();
    };
  }

  setInterval(() => {
    if (mode === "sse" && lastApply && Date.now() - lastApply > 2500) {
      if (esRef) {
        try { esRef.close(); } catch (_) {}
        esRef = null;
      }
      startPoll();
    }
  }, 1000);

  startSSE();
})();
</script>
</body>
</html>
"""


def render_page_html(room: str = DEFAULT_ROOM) -> str:
    """Fill room name into the mobile page."""
    name = (room or DEFAULT_ROOM).strip() or DEFAULT_ROOM
    safe = (
        name.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return PAGE_HTML.replace("{{ROOM}}", safe)


class PresenceHub:
    """Thread-safe latest snapshot for HTTP / SSE clients."""

    def __init__(self, *, trained_order: list[str] | None = None) -> None:
        self._lock = threading.Lock()
        self._tcp_status: dict[str, Any] = {"active": 0, "ips": []}
        self._trained_order: list[str] = list(trained_order or [])
        self._per_rx: dict[str, dict[str, Any]] = {}
        self._detect: dict[str, Any] = {
            "ready": False,
            "state": "waiting",
            "score": None,
            "threshold": None,
            "p_presence": None,
            "rx_present": None,
            "rx_total": None,
            "seq": None,
            "rssi": None,
            "ts": None,
        }
        self._cond = threading.Condition(self._lock)
        self._gen = 0

    @property
    def tcp_status(self) -> dict[str, Any]:
        return self._tcp_status

    def set_trained_order(self, order: list[str] | None) -> None:
        with self._lock:
            self._trained_order = list(order or [])

    def note_packet(self, source_id: str | None, meta: dict[str, Any]) -> None:
        """Record last CSI sighting for a TCP client IP."""
        sid = (source_id or meta.get("source_id") or "").strip()
        if not sid:
            return
        with self._lock:
            self._per_rx[sid] = {
                "rssi": meta.get("rssi"),
                "seq": meta.get("seq"),
                "last_mono": time.monotonic(),
            }

    def update_detect(self, result: dict[str, Any], meta: dict[str, Any]) -> None:
        with self._cond:
            snap = {
                "ready": bool(result.get("ready")),
                "state": result.get("state", "waiting"),
                "score": result.get("score"),
                "threshold": result.get("threshold"),
                "p_presence": result.get(
                    "p_presence", result.get("p_object")
                ),
                "rx_present": result.get("rx_present"),
                "rx_total": result.get("rx_total"),
                "rx_ready": result.get("rx_ready"),
                "rx_need": result.get("rx_need"),
                "rx_missing": result.get("rx_missing"),
                "buffered": result.get("buffered"),
                "need": result.get("need"),
                "seq": meta.get("seq"),
                "rssi": meta.get("rssi"),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            self._detect = snap
            self._gen += 1
            self._cond.notify_all()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_unlocked()

    def _devices_unlocked(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        tcp_ips = {str(ip) for ip in (self._tcp_status.get("ips") or [])}
        trained = set(self._trained_order)
        all_ips = sorted(tcp_ips | trained | set(self._per_rx.keys()))
        devices: list[dict[str, Any]] = []
        for ip in all_ips:
            info = self._per_rx.get(ip) or {}
            age_s = None
            last = info.get("last_mono")
            if last is not None:
                age_s = round(now - float(last), 1)
            recent = age_s is not None and age_s <= DEVICE_LIVE_GRACE_S
            connected = ip in tcp_ips or recent
            devices.append(
                {
                    "ip": ip,
                    "connected": connected,
                    "trained": ip in trained,
                    "rssi": info.get("rssi"),
                    "seq": info.get("seq"),
                    "age_s": age_s,
                }
            )
        devices.sort(key=lambda d: (not d["connected"], d["ip"]))
        return devices

    def _snapshot_unlocked(self) -> dict[str, Any]:
        tcp = dict(self._tcp_status)
        out = dict(self._detect)
        devices = self._devices_unlocked()
        live_ips = [d["ip"] for d in devices if d["connected"]]
        out["esp_active"] = len(live_ips)
        out["esp_connections"] = int(tcp.get("connections") or 0)
        out["esp_ips"] = live_ips
        out["trained_ips"] = list(self._trained_order)
        out["devices"] = devices
        return out

    def wait_snapshot(self, last_gen: int, timeout: float = 25.0) -> tuple[int, dict[str, Any]]:
        with self._cond:
            if self._gen == last_gen:
                self._cond.wait(timeout=timeout)
            return self._gen, self._snapshot_unlocked()


def _make_handler(hub: PresenceHub, *, room: str = DEFAULT_ROOM):
    page = render_page_html(room)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            if args and str(args[0]).startswith(("4", "5")):
                super().log_message(fmt, *args)

        def handle(self) -> None:
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/status":
                payload = json.dumps(hub.snapshot()).encode("utf-8")
                self._send(200, payload, "application/json")
                return
            if path == "/api/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                gen = -1
                try:
                    while True:
                        gen, snap = hub.wait_snapshot(gen, timeout=0.25)
                        self.wfile.write(f"data: {json.dumps(snap)}\n\n".encode("utf-8"))
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            self._send(404, b'{"error":"not found"}\n', "application/json")

    return Handler


def _csi_loop(
    hub: PresenceHub,
    detector,
    *,
    listen_tcp: int,
    stop: threading.Event,
) -> None:
    """Read CSI forever; bind failures retry. Never kill the HTTP UI."""
    pkt = 0
    last_log = time.monotonic()
    last_pkt = time.monotonic()
    while not stop.is_set():
        try:
            for item in iter_csi_from_tcp(
                listen_tcp, status=hub.tcp_status
            ):
                if stop.is_set():
                    return
                if item is None:
                    now = time.monotonic()
                    if now - last_log >= 5.0:
                        snap = hub.snapshot()
                        idle = now - last_pkt
                        print(
                            f"web csi: {pkt} pkts  boards={snap.get('esp_active')}  "
                            f"state={snap.get('state')!r}  "
                            f"idle={idle:.0f}s  (waiting)",
                            flush=True,
                        )
                        last_log = now
                    continue
                source_id, iq, meta = item
                hub.note_packet(source_id, meta)
                kwargs = dict(
                    rssi=float(meta.get("rssi") or 0.0),
                    agc_gain=float(meta.get("agc_gain") or 0.0),
                    fft_gain=float(meta.get("fft_gain") or 0.0),
                    seq=meta.get("seq"),
                )
                if isinstance(detector, MultiRxLiveDetector):
                    result = detector.on_packet(
                        iq, source_id=source_id or meta.get("source_id"), **kwargs
                    )
                else:
                    result = detector.on_packet(iq, **kwargs)
                if result is None:
                    now = time.monotonic()
                    if now - last_log >= 5.0:
                        snap = hub.snapshot()
                        idle = now - last_pkt
                        print(
                            f"web csi: {pkt} pkts  boards={snap.get('esp_active')}  "
                            f"state={snap.get('state')!r}  "
                            f"idle={idle:.0f}s  (buffering)",
                            flush=True,
                        )
                        last_log = now
                    continue
                hub.update_detect(result, meta)
                pkt += 1
                last_pkt = time.monotonic()
                now = last_pkt
                if now - last_log >= 5.0:
                    snap = hub.snapshot()
                    print(
                        f"web csi: {pkt} pkts  boards={snap.get('esp_active')}  "
                        f"state={snap.get('state')!r}  "
                        f"score={snap.get('score')}",
                        flush=True,
                    )
                    last_log = now
            # Generator ended without error — restart acceptor.
            if stop.is_set():
                return
            print(
                f"web csi: TCP iterator ended; re-listen :{listen_tcp} in 1s…",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(1.0)
        except OSError as exc:
            print(
                f"TCP :{listen_tcp} failed: {exc}. "
                "Retrying in 2s (stop ingest/live/gui if they hold the port).",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(2.0)


def main(argv: list[str] | None = None) -> None:
    # Leave the SSH controlling TTY so job-control STOP can't freeze CSI/UI.
    import os

    try:
        os.setsid()
    except OSError:
        pass
    for sig_name in ("SIGTTOU", "SIGTTIN"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, signal.SIG_IGN)
        except (OSError, ValueError):
            pass

    parser = build_live_arg_parser(include_terminal_flags=True)
    parser.add_argument(
        "--http-port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f"Phone UI HTTP port (default {DEFAULT_HTTP_PORT})",
    )
    parser.add_argument(
        "--http-bind",
        default="0.0.0.0",
        help="HTTP bind address (default 0.0.0.0)",
    )
    parser.add_argument(
        "--room",
        default=DEFAULT_ROOM,
        help=f'Room label shown at top of phone UI (default "{DEFAULT_ROOM}")',
    )
    args = parser.parse_args(argv)
    if getattr(args, "gui", False):
        print("NOTE: --gui ignored by detect_web (use ./run_presence.sh gui)", file=sys.stderr)

    if args.listen_tcp is None and not args.port and not args.from_file:
        args.listen_tcp = 9055

    if args.listen_tcp is None:
        sys.exit(
            "detect_web requires --listen-tcp (multi-C5 fan-in). "
            "Example: --listen-tcp 9055"
        )

    bundle, calibration, cal_path = load_bundle_and_calibration(
        args.model,
        calibration_path=args.calibration,
        no_calibration=args.no_calibration,
    )
    detector = make_live_detector(
        bundle,
        threshold=args.threshold,
        fast=args.fast,
        calibration=calibration,
        rx_min=getattr(args, "rx_min", "auto"),
    )
    print_startup_banner(
        bundle,
        detector,
        calibration=calibration,
        cal_path=cal_path,
        fast=args.fast,
        stop_hint=(
            f"Phone UI: http://<mini-ip>:{args.http_port}  "
            "(stop ingest/live/gui first — CSI uses :9055)"
        ),
    )

    trained = list(
        getattr(detector, "rx_sources_order", None)
        or bundle.get("rx_sources_order")
        or []
    )
    hub = PresenceHub(trained_order=trained)
    stop = threading.Event()
    reader = threading.Thread(
        target=_csi_loop,
        args=(hub, detector),
        kwargs={"listen_tcp": args.listen_tcp, "stop": stop},
        name="csi-web-reader",
        daemon=True,
    )
    reader.start()

    handler = _make_handler(hub, room=args.room)
    httpd = ThreadingHTTPServer((args.http_bind, args.http_port), handler)
    print(
        f"web UI http://{args.http_bind}:{args.http_port}/  room={args.room!r}  "
        f"(CSI tcp :{args.listen_tcp}; trained RXs={trained or ['(single)']})",
        flush=True,
    )
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nstopped.", flush=True)
    finally:
        stop.set()
        httpd.server_close()


if __name__ == "__main__":
    main()
