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

PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<title>Presence</title>
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
  header {
    display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 12px;
  }
  header h1 {
    font-size: 0.85rem; font-weight: 600; letter-spacing: 0.08em;
    text-transform: uppercase; color: var(--muted); margin: 0;
  }
  #conn {
    font-size: 0.75rem; color: var(--muted);
  }
  #conn.ok { color: var(--empty-fg); }
  #conn.bad { color: var(--presence-fg); }
  #state {
    flex: 1; display: flex; align-items: center; justify-content: center;
    border-radius: 20px; margin: 8px 0 16px;
    font-size: clamp(2.8rem, 14vw, 5rem); font-weight: 700;
    letter-spacing: 0.04em; transition: background 0.25s, color 0.25s;
    background: var(--wait); color: var(--wait-fg);
    min-height: 28vh;
  }
  body.empty #state { background: var(--empty); color: var(--empty-fg); }
  body.presence #state { background: var(--presence); color: var(--presence-fg); }
  body.waiting #state { background: var(--wait); color: var(--wait-fg); }
  .panel { display: grid; gap: 10px; }
  .row {
    display: flex; justify-content: space-between; gap: 12px;
    padding: 12px 14px; border-radius: 12px;
    background: rgba(255,255,255,0.04);
    font-size: 0.95rem;
  }
  .row .k { color: var(--muted); }
  .row .v { font-variant-numeric: tabular-nums; text-align: right; }
  .devices {
    margin-top: 4px;
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
  footer {
    margin-top: 16px; font-size: 0.7rem; color: var(--muted);
    text-align: center;
  }
</style>
</head>
<body class="waiting">
<header>
  <h1>CSI presence</h1>
  <span id="conn">connecting…</span>
</header>
<div id="state">WAITING</div>
<div class="panel">
  <div class="row"><span class="k">Score / thr</span><span class="v" id="score">—</span></div>
  <div class="row"><span class="k">RX fused</span><span class="v" id="rx">—</span></div>
  <div class="row"><span class="k">RSSI / seq</span><span class="v" id="meta">—</span></div>
  <div class="devices">
    <h2>
      <span>Connected to Mini</span>
      <span id="esp">0 live</span>
    </h2>
    <ul id="device-list"></ul>
    <div id="device-empty">No ESP TCP clients on :9055 yet</div>
  </div>
</div>
<footer>LabPSK · TCP :9055 → Mini · live board list updates automatically</footer>
<script>
(function () {
  const body = document.body;
  const el = (id) => document.getElementById(id);

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

    if (s.score != null && s.threshold != null) {
      const sc = Number(s.score);
      const thr = Number(s.threshold);
      el("score").textContent =
        (sc >= 0 ? "+" : "") + sc.toFixed(3) + " / " +
        (thr >= 0 ? "+" : "") + thr.toFixed(3);
    } else {
      el("score").textContent = "—";
    }

    if (s.rx_present != null) {
      el("rx").textContent = s.rx_present + "/" + (s.rx_total ?? "?");
    } else if (s.rx_ready != null) {
      el("rx").textContent = s.rx_ready + "/" + (s.rx_need ?? "?") + " ready";
    } else {
      el("rx").textContent = "—";
    }

    const bits = [];
    if (s.rssi != null) bits.push(s.rssi + " dBm");
    if (s.seq != null) bits.push("seq " + s.seq);
    el("meta").textContent = bits.length ? bits.join(" · ") : "—";

    renderDevices(s);
  }

  function fail() {
    el("conn").textContent = "offline";
    el("conn").className = "bad";
  }

  function connect() {
    if (!window.EventSource) {
      poll();
      return;
    }
    const es = new EventSource("/api/stream");
    es.onmessage = (ev) => {
      try { apply(JSON.parse(ev.data)); } catch (_) {}
    };
    es.onerror = () => {
      fail();
      es.close();
      setTimeout(connect, 1500);
    };
  }

  function poll() {
    fetch("/api/status")
      .then((r) => r.json())
      .then(apply)
      .catch(fail)
      .finally(() => setTimeout(poll, 400));
  }

  connect();
})();
</script>
</body>
</html>
"""


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
            connected = ip in tcp_ips
            age_s = None
            last = info.get("last_mono")
            if last is not None:
                age_s = round(now - float(last), 1)
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
        out["esp_active"] = int(tcp.get("active") or 0)
        out["esp_ips"] = list(tcp.get("ips") or [])
        out["trained_ips"] = list(self._trained_order)
        out["devices"] = self._devices_unlocked()
        return out

    def wait_snapshot(self, last_gen: int, timeout: float = 25.0) -> tuple[int, dict[str, Any]]:
        with self._cond:
            if self._gen == last_gen:
                self._cond.wait(timeout=timeout)
            return self._gen, self._snapshot_unlocked()


def _make_handler(hub: PresenceHub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            # Keep CSI terminal readable — only log errors.
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
                self._send(200, PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/status":
                payload = json.dumps(hub.snapshot()).encode("utf-8")
                self._send(200, payload, "application/json")
                return
            if path == "/api/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                gen = -1
                try:
                    while True:
                        # Short timeout so ESP connect/disconnect shows without
                        # waiting for the next CSI packet.
                        gen, snap = hub.wait_snapshot(gen, timeout=0.4)
                        line = f"data: {json.dumps(snap)}\n\n"
                        self.wfile.write(line.encode("utf-8"))
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
    try:
        for source_id, iq, meta in iter_csi_from_tcp(
            listen_tcp, status=hub.tcp_status
        ):
            if stop.is_set():
                break
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
                continue
            hub.update_detect(result, meta)
    except OSError as exc:
        print(
            f"TCP :{listen_tcp} failed: {exc}. "
            "Stop ./run_multi_ingest.sh / terminal live / gui first.",
            file=sys.stderr,
            flush=True,
        )
        stop.set()


def main(argv: list[str] | None = None) -> None:
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
    # detect_live's --gui is irrelevant here; ignore if forwarded.
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

    handler = _make_handler(hub)
    httpd = ThreadingHTTPServer((args.http_bind, args.http_port), handler)
    print(
        f"web UI http://{args.http_bind}:{args.http_port}/  "
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
