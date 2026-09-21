#!/usr/bin/env python3
"""Vigorous tests for the mobile presence web viewer (no hardware).

Run from repo:
  cd presence_detection && ./run_presence.sh test-web
  # or:
  cd csi_pipeline_new && source ./uv_common.sh && \\
    uv_csi ../presence_detection/tests/test_web_live.py
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

PRESENCE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PRESENCE_ROOT.parent
CSI = REPO_ROOT / "csi_pipeline_new"
sys.path.insert(0, str(CSI))

from detect_web import (  # noqa: E402
    DEFAULT_HTTP_PORT,
    PAGE_HTML,
    PresenceHub,
    _make_handler,
    render_page_html,
)
from ingest_serial import iter_lines_tcp  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_get(url: str, *, timeout: float = 3.0) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        headers = {k.lower(): v for k, v in resp.headers.items()}
        return resp.status, headers, resp.read()


class TestPresenceHub(unittest.TestCase):
    def test_initial_snapshot(self) -> None:
        hub = PresenceHub()
        s = hub.snapshot()
        self.assertFalse(s["ready"])
        self.assertEqual(s["esp_active"], 0)
        self.assertEqual(s["esp_ips"], [])
        self.assertEqual(s["state"], "waiting")

    def test_update_detect_fields(self) -> None:
        hub = PresenceHub()
        hub.update_detect(
            {
                "ready": True,
                "state": "presence",
                "score": 4.2,
                "threshold": 3.9,
                "p_presence": 0.97,
                "rx_present": 3,
                "rx_total": 3,
            },
            {"seq": 99, "rssi": -52},
        )
        s = hub.snapshot()
        self.assertTrue(s["ready"])
        self.assertEqual(s["state"], "presence")
        self.assertEqual(s["score"], 4.2)
        self.assertEqual(s["threshold"], 3.9)
        self.assertEqual(s["p_presence"], 0.97)
        self.assertEqual(s["rx_present"], 3)
        self.assertEqual(s["rx_total"], 3)
        self.assertEqual(s["seq"], 99)
        self.assertEqual(s["rssi"], -52)
        self.assertIsNotNone(s["ts"])

    def test_p_object_fallback(self) -> None:
        hub = PresenceHub()
        hub.update_detect(
            {"ready": True, "state": "empty", "p_object": 0.4, "threshold": 0.5},
            {},
        )
        self.assertEqual(hub.snapshot()["p_presence"], 0.4)

    def test_tcp_status_reflected_in_snapshot(self) -> None:
        hub = PresenceHub(
            trained_order=["10.128.93.29", "10.128.93.31", "10.128.93.32"]
        )
        hub.tcp_status["active"] = 2
        hub.tcp_status["ips"] = ["10.128.93.29", "10.128.93.31"]
        hub.note_packet("10.128.93.29", {"rssi": -48, "seq": 10})
        s = hub.snapshot()
        self.assertEqual(s["esp_active"], 2)
        self.assertEqual(len(s["esp_ips"]), 2)
        devices = {d["ip"]: d for d in s["devices"]}
        self.assertTrue(devices["10.128.93.29"]["connected"])
        self.assertTrue(devices["10.128.93.29"]["trained"])
        self.assertEqual(devices["10.128.93.29"]["rssi"], -48)
        self.assertTrue(devices["10.128.93.31"]["connected"])
        self.assertFalse(devices["10.128.93.32"]["connected"])
        self.assertTrue(devices["10.128.93.32"]["trained"])

    def test_devices_sort_live_first(self) -> None:
        hub = PresenceHub(trained_order=["10.0.0.2", "10.0.0.1"])
        hub.tcp_status["active"] = 1
        hub.tcp_status["ips"] = ["10.0.0.2"]
        ips = [d["ip"] for d in hub.snapshot()["devices"]]
        self.assertEqual(ips[0], "10.0.0.2")

    def test_recent_packet_keeps_live_through_tcp_gap(self) -> None:
        hub = PresenceHub(trained_order=["10.128.93.29"])
        hub.note_packet("10.128.93.29", {"rssi": -50, "seq": 1})
        hub.tcp_status["ips"] = []
        hub.tcp_status["connections"] = 0
        d = {x["ip"]: x for x in hub.snapshot()["devices"]}
        self.assertTrue(d["10.128.93.29"]["connected"])
        self.assertEqual(hub.snapshot()["esp_active"], 1)

    def test_wait_snapshot_timeout_no_deadlock(self) -> None:
        hub = PresenceHub()
        t0 = time.monotonic()
        gen, snap = hub.wait_snapshot(-1, timeout=0.15)
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(gen, 0)
        self.assertIn("esp_active", snap)
        self.assertLess(elapsed, 1.0)

    def test_wait_snapshot_wakes_on_update(self) -> None:
        hub = PresenceHub()
        done: list[tuple[int, dict]] = []

        def waiter() -> None:
            done.append(hub.wait_snapshot(0, timeout=5.0))

        th = threading.Thread(target=waiter, daemon=True)
        th.start()
        time.sleep(0.05)
        hub.update_detect(
            {"ready": True, "state": "empty", "score": 1.0, "threshold": 2.0},
            {"seq": 1},
        )
        th.join(timeout=2.0)
        self.assertFalse(th.is_alive())
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0][1]["state"], "empty")
        self.assertEqual(done[0][1]["seq"], 1)

    def test_concurrent_readers(self) -> None:
        hub = PresenceHub()
        errors: list[str] = []

        def reader(n: int) -> None:
            try:
                for i in range(40):
                    hub.update_detect(
                        {
                            "ready": True,
                            "state": "empty" if i % 2 == 0 else "presence",
                            "score": float(i),
                            "threshold": 3.0,
                            "rx_present": 2,
                            "rx_total": 3,
                        },
                        {"seq": n * 1000 + i, "rssi": -50},
                    )
                    s = hub.snapshot()
                    assert "esp_active" in s
                    gen, s2 = hub.wait_snapshot(0, timeout=0.01)
                    assert isinstance(gen, int)
                    assert "state" in s2
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

        threads = [threading.Thread(target=reader, args=(i,), daemon=True) for i in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)
        self.assertEqual(errors, [])


class TestPageHtml(unittest.TestCase):
    def test_page_has_mobile_essentials(self) -> None:
        for needle in (
            "viewport",
            "/api/status",
            "/api/stream",
            "EventSource",
            "Connected to Mini",
            "device-list",
            "{{ROOM}}",
            'id="line"',
            "PRESENCE",
            "EMPTY",
            "startSSE",
        ):
            self.assertIn(needle, PAGE_HTML, msg=f"missing {needle!r}")

    def test_render_page_injects_room(self) -> None:
        html = render_page_html("Room 207")
        self.assertIn("Room 207", html)
        self.assertNotIn("{{ROOM}}", html)
        self.assertIn('id="line"', html)

    def test_default_http_port(self) -> None:
        self.assertEqual(DEFAULT_HTTP_PORT, 8765)


class TestHttpApi(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = PresenceHub(
            trained_order=["10.128.93.29", "10.128.93.31", "10.128.93.32"]
        )
        self.hub.tcp_status["active"] = 2
        self.hub.tcp_status["ips"] = ["10.128.93.29", "10.128.93.31"]
        self.hub.note_packet("10.128.93.29", {"rssi": -48, "seq": 41})
        self.hub.update_detect(
            {
                "ready": True,
                "state": "empty",
                "score": 1.5,
                "threshold": 3.95,
                "p_presence": 0.82,
                "rx_present": 2,
                "rx_total": 3,
            },
            {"seq": 42, "rssi": -55},
        )
        port = _free_port()
        handler = _make_handler(self.hub, room="Room 207")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.base = f"http://127.0.0.1:{port}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        time.sleep(0.05)

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_index_html(self) -> None:
        status, headers, body = _http_get(self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("content-type", ""))
        text = body.decode("utf-8")
        self.assertIn("Room 207", text)
        self.assertIn('id="line"', text)
        self.assertIn("EventSource", text)

    def test_index_html_alias(self) -> None:
        status, _, body = _http_get(self.base + "/index.html")
        self.assertEqual(status, 200)
        self.assertIn(b"Connected to Mini", body)

    def test_api_status_json(self) -> None:
        status, headers, body = _http_get(self.base + "/api/status")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("content-type", ""))
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["state"], "empty")
        self.assertEqual(data["esp_active"], 2)
        self.assertEqual(data["esp_ips"], ["10.128.93.29", "10.128.93.31"])
        self.assertEqual(data["score"], 1.5)
        self.assertEqual(data["threshold"], 3.95)
        self.assertEqual(data["rx_present"], 2)
        self.assertEqual(data["rx_total"], 3)
        self.assertEqual(data["seq"], 42)
        devices = {d["ip"]: d for d in data["devices"]}
        self.assertTrue(devices["10.128.93.29"]["connected"])
        self.assertFalse(devices["10.128.93.32"]["connected"])
        self.assertIn("devices", data)

    def test_api_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _http_get(self.base + "/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_api_stream_sse(self) -> None:
        req = urllib.request.Request(self.base + "/api/stream")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            self.assertEqual(resp.status, 200)
            ctype = resp.headers.get("Content-Type", "")
            self.assertIn("text/event-stream", ctype)
            # Read until first complete SSE event.
            buf = b""
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline and b"\n\n" not in buf:
                chunk = resp.read(64)
                if not chunk:
                    break
                buf += chunk
        self.assertIn(b"data: ", buf)
        line = buf.split(b"\n\n", 1)[0].decode("utf-8")
        self.assertTrue(line.startswith("data: "))
        data = json.loads(line[len("data: ") :])
        self.assertEqual(data["esp_active"], 2)
        self.assertIn(data["state"], ("empty", "presence", "waiting"))

    def test_concurrent_status_clients(self) -> None:
        results: list[int] = []
        errors: list[str] = []

        def one() -> None:
            try:
                st, _, body = _http_get(self.base + "/api/status")
                results.append(st)
                json.loads(body.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

        threads = [threading.Thread(target=one, daemon=True) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)
        self.assertEqual(errors, [])
        self.assertEqual(results, [200] * 8)


class TestTcpClientStatus(unittest.TestCase):
    def test_status_tracks_connect_and_disconnect(self) -> None:
        port = _free_port()
        status: dict = {}
        stop = threading.Event()
        lines: list = []

        def consumer() -> None:
            for item in iter_lines_tcp(port, bind="127.0.0.1", status=status):
                if stop.is_set():
                    break
                if item is not None:
                    lines.append(item)

        th = threading.Thread(target=consumer, daemon=True)
        th.start()

        # Wait until listener is up, then drain the probe connection.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    pass
                break
            except OSError:
                time.sleep(0.05)
        else:
            stop.set()
            self.fail("TCP listener did not start")

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and status.get("active", -1) != 0:
            time.sleep(0.05)
        self.assertEqual(status.get("active", 0), 0)

        # Fresh connections for the actual test.
        c1 = socket.create_connection(("127.0.0.1", port), timeout=1.0)
        c2 = socket.create_connection(("127.0.0.1", port), timeout=1.0)
        time.sleep(0.15)
        # Same host IP can hold 2 sockets; board count stays 1.
        self.assertEqual(status.get("active"), 1)
        self.assertEqual(status.get("connections"), 2)
        self.assertIn("127.0.0.1", status.get("ips") or [])

        c1.sendall(b'CSI_DATA,1,aa:bb:cc:dd:ee:ff,-40,11,-90,0,0,1,1,8,0,8,1,"[1,2,3,4,5,6,7,8]"\n')
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not lines:
            time.sleep(0.05)
        self.assertTrue(lines, "expected CSI line from client")
        self.assertEqual(lines[0][0], "127.0.0.1")

        c1.close()
        c2.close()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and status.get("connections", -1) != 0:
            time.sleep(0.05)
        self.assertEqual(status.get("connections"), 0)
        self.assertEqual(status.get("active"), 0)
        self.assertEqual(status.get("ips"), [])
        stop.set()


class TestFrontDoorWiring(unittest.TestCase):
    def test_run_presence_help_lists_web(self) -> None:
        r = subprocess.run(
            [str(PRESENCE_ROOT / "run_presence.sh")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertIn("web", r.stdout)
        self.assertIn("8765", r.stdout)

    def test_run_detect_routes_web(self) -> None:
        text = (CSI / "run_detect.sh").read_text(encoding="utf-8")
        self.assertIn("detect_web.py", text)
        self.assertIn("--web", text)

    def test_detect_web_module_imports(self) -> None:
        r = subprocess.run(
            [
                "bash",
                "-lc",
                f"source '{CSI}/uv_common.sh' && uv_csi -c 'import detect_web; print(detect_web.DEFAULT_HTTP_PORT)'",
            ],
            capture_output=True,
            text=True,
            cwd=str(CSI),
            check=False,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr[-500:])
        self.assertIn("8765", r.stdout)


if __name__ == "__main__":
    print("=== presence_detection web live tests ===")
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
