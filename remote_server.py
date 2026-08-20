#!/usr/bin/env python3

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8000
lock = threading.Lock()
latest = {"jpeg": b"", "t": 0.0}

PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Remote camera</title>
<style>
  body { margin: 0; background: #111; color: #eee; font-family: sans-serif; text-align: center; }
  img { width: min(100vw, 640px); background: #000; margin-top: 12px; }
</style>
</head>
<body>
<h2>Remote camera</h2>
<p id="s">Waiting for frames from the ESP…</p>
<img id="v" alt="live">
<script>
const v = document.getElementById('v');
const s = document.getElementById('s');
async function tick() {
  try {
    const r = await fetch('/latest.jpg?t=' + Date.now(), {
      headers: { 'ngrok-skip-browser-warning': '1' }
    });
    if (!r.ok) throw new Error('no frame');
    const b = await r.blob();
    if (v.src && v.src.startsWith('blob:')) URL.revokeObjectURL(v.src);
    v.src = URL.createObjectURL(b);
    s.textContent = 'Live';
  } catch (e) {
    s.textContent = 'Waiting for frames from the ESP…';
  }
  setTimeout(tick, 80);
}
tick();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        if args and "latest.jpg" in str(args[0]):
            return
        super().log_message(fmt, *args)

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/latest.jpg"):
            with lock:
                data = latest["jpeg"]
            if not data:
                self.send_error(404, "no frame yet")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/upload":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(n)
        if len(data) < 2 or data[:2] != b"\xff\xd8":
            self.send_error(400, "expected JPEG")
            return
        with lock:
            latest["jpeg"] = data
            latest["t"] = time.time()
        self.send_response(204)
        self.end_headers()


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Viewer: http://127.0.0.1:{PORT}")
    print("Keep this running. Start ngrok in another terminal: ngrok http 8000")
    server.serve_forever()


if __name__ == "__main__":
    main()
