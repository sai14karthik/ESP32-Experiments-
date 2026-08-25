#!/usr/bin/env -S uv run python
"""Live view over USB. Laptop and ESP can be on different Wi-Fi; video uses the cable."""

from __future__ import annotations

import glob
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import serial

BAUD = 921600
MAGIC = b"CAM0"
PORT_HTTP = 8000

lock = threading.Lock()
latest = {"jpeg": b""}

PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>USB live camera</title>
<style>
  body { margin: 0; background: #111; color: #eee; font-family: sans-serif; text-align: center; }
  img { width: min(100vw, 640px); background: #000; margin-top: 12px; }
</style>
</head>
<body>
<h2>USB live camera</h2>
<p id="s">Waiting for USB frames…</p>
<img id="v" alt="live">
<script>
const v = document.getElementById('v');
const s = document.getElementById('s');
function tick() { v.src = '/latest.jpg?t=' + Date.now(); }
v.onload = () => { s.textContent = 'Live (USB)'; setTimeout(tick, 40); };
v.onerror = () => { s.textContent = 'Waiting for USB frames…'; setTimeout(tick, 400); };
tick();
</script>
</body>
</html>
"""


def find_port() -> str:
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    if not ports:
        raise SystemExit("No /dev/cu.usbmodem* — plug in the XIAO USB cable.")
    return ports[0]


def read_exact(ser: serial.Serial, n: int):
    buf = bytearray()
    while len(buf) < n:
        chunk = ser.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def sync_frame(ser: serial.Serial):
    window = bytearray()
    deadline = time.time() + 8
    while time.time() < deadline:
        chunk = ser.read(256)
        if not chunk:
            continue
        window += chunk
        while True:
            idx = window.find(MAGIC)
            if idx < 0:
                window = window[-3:]
                break
            window = window[idx + 4 :]
            while len(window) < 4:
                extra = ser.read(4 - len(window))
                if not extra:
                    return None
                window += extra
            (length,) = struct.unpack("<I", window[:4])
            window = window[4:]
            if not (1000 < length < 200_000):
                continue
            payload = bytes(window[:length])
            window = window[length:]
            missing = length - len(payload)
            if missing:
                rest = read_exact(ser, missing)
                if not rest:
                    return None
                payload += rest
            if payload.startswith(b"\xff\xd8"):
                return payload
    return None


def serial_loop(port: str) -> None:
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = BAUD
    ser.timeout = 1
    ser.dtr = False
    ser.rts = False
    ser.open()
    time.sleep(1.5)
    ser.reset_input_buffer()
    print(f"Reading frames from {port}")
    while True:
        jpeg = sync_frame(ser)
        if not jpeg:
            continue
        with lock:
            latest["jpeg"] = jpeg


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
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)


def main() -> None:
    port = find_port()
    threading.Thread(target=serial_loop, args=(port,), daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT_HTTP), Handler)
    print(f"Open http://127.0.0.1:{PORT_HTTP}")
    print("Close Arduino Serial Monitor first. CameraSerial firmware must be on the board.")
    server.serve_forever()


if __name__ == "__main__":
    main()
