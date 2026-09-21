#!/usr/bin/env python3
"""End-to-end presence pipeline proof (synthetic; no hardware).

Covers: front door → N=1/N=3 train → calibrate → live (1..N, hotplug, rx-min)
→ web hub/HTTP device list → multirx fusion suite.

  cd csi_pipeline_new && source ./uv_common.sh && uv_csi test_presence_e2e.py
  # or:
  cd presence_detection && ./run_presence.sh test-e2e
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import joblib

ROOT = Path(__file__).resolve().parent
PRES = ROOT.parent / "presence_detection"
sys.path.insert(0, str(ROOT))

from csi_features import configure_from_iq_len  # noqa: E402
from detect_live import (  # noqa: E402
    LiveDetector,
    MultiRxLiveDetector,
    format_line,
    make_live_detector,
)
from detect_web import PresenceHub, _make_handler  # noqa: E402
from ingest_serial import iter_lines_tcp  # noqa: E402
from test_multirx_fusion import IQ_LEN, _iq, write_synthetic_csv  # noqa: E402

failures: list[str] = []


def ok(msg: str) -> None:
    print(f"  OK  {msg}")


def fail(msg: str) -> None:
    print(f"  FAIL {msg}")
    failures.append(msg)


def run(cmd: list[str], *, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd))


def feed(det, sources, bundle, t0: float, *, occupied: bool = False):
    last = None
    for k in range(int(bundle["window_size"]) + 5):
        for src in sources:
            iq = [int(x) for x in _iq(k * 11 + hash(src) % 17, occupied).split(",")]
            last = det.on_packet(
                iq,
                source_id=src,
                rssi=-48,
                agc_gain=10,
                fft_gain=20,
                seq=k,
                arrival=t0 + k * 0.08,
            )
    return last


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _check_web_stack(bundle: dict, sources: list[str], cal: dict | None) -> None:
    """A–Z: hub ← TCP status + packets ← HTTP /api/status + SSE essentials."""
    print("=== web live path ===")
    hub = PresenceHub(trained_order=list(bundle.get("rx_sources_order") or sources))
    det = make_live_detector(bundle, fast=True, calibration=cal, rx_min="auto")
    assert isinstance(det, MultiRxLiveDetector)

    hub.tcp_status["active"] = 3
    hub.tcp_status["ips"] = list(sources)
    last = None
    for k in range(int(bundle["window_size"]) + 5):
        for src in sources:
            iq = [int(x) for x in _iq(k * 11 + hash(src) % 17, False).split(",")]
            meta = {"seq": k, "rssi": -50, "source_id": src}
            hub.note_packet(src, meta)
            last = det.on_packet(
                iq,
                source_id=src,
                rssi=-50,
                agc_gain=10,
                fft_gain=20,
                seq=k,
                arrival=9000.0 + k * 0.08,
            )
            if last is not None:
                hub.update_detect(last, meta)

    snap = hub.snapshot()
    if snap.get("esp_active") != 3:
        fail(f"web snap esp_active={snap.get('esp_active')}")
    else:
        ok("web hub esp_active=3")
    devices = {d["ip"]: d for d in snap.get("devices") or []}
    if not all(devices.get(s, {}).get("connected") for s in sources):
        fail(f"web devices not all LIVE: {snap.get('devices')}")
    else:
        ok(f"web devices LIVE={list(devices)}")
    if not snap.get("ready"):
        fail(f"web detect not ready: {snap}")
    else:
        state = snap.get("state")
        if state not in ("empty", "presence", "object"):
            fail(f"web bad state={state}")
        else:
            ok(f"web detect state={state} rx={snap.get('rx_present')}/{snap.get('rx_total')}")

    hub.tcp_status["active"] = 2
    hub.tcp_status["ips"] = sources[:2]
    snap2 = hub.snapshot()
    dmap = {d["ip"]: d for d in snap2["devices"]}
    if dmap[sources[2]]["connected"] or not dmap[sources[0]]["connected"]:
        fail(f"web disconnect reflect: {snap2['devices']}")
    else:
        ok("web disconnect → OFF for dropped RX")

    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(hub))
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    time.sleep(0.05)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as resp:
            html = resp.read().decode("utf-8")
        if "Connected to Mini" not in html or "device-list" not in html:
            fail("web HTML missing device panel")
        elif "Room 207" not in html:
            fail("web HTML missing room label")
        else:
            ok("web HTML has room + Connected to Mini")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if "devices" not in data or data.get("esp_active") != 2:
            fail(f"web /api/status: {data}")
        else:
            ok(
                f"web /api/status esp_active={data['esp_active']} "
                f"devices={len(data['devices'])}"
            )
    finally:
        httpd.shutdown()
        httpd.server_close()

    csi_port = _free_port()
    status: dict = {}
    stop = threading.Event()
    got: list = []

    def consumer() -> None:
        for item in iter_lines_tcp(csi_port, bind="127.0.0.1", status=status):
            if stop.is_set():
                break
            if item is not None:
                got.append(item)

    threading.Thread(target=consumer, daemon=True).start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", csi_port), timeout=0.2):
                pass
            break
        except OSError:
            time.sleep(0.05)
    else:
        stop.set()
        fail("web TCP fan-in did not listen")
        return
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and status.get("active", -1) != 0:
        time.sleep(0.05)

    socks = [
        socket.create_connection(("127.0.0.1", csi_port), timeout=1.0)
        for _ in range(3)
    ]
    time.sleep(0.15)
    if status.get("connections") != 3 or status.get("active") != 1:
        fail(
            f"web TCP sockets={status.get('connections')} "
            f"boards={status.get('active')} (expect 3 sockets / 1 local IP)"
        )
    else:
        ok("web TCP fan-in sockets=3 boards=1 (localhost)")
        hub2 = PresenceHub(trained_order=sources)
        hub2.tcp_status.update(status)
        if hub2.snapshot()["esp_active"] != 1:
            fail("hub tcp_status bridge failed")
        else:
            ok("web hub ← TCP status bridge")
    for s in socks:
        s.close()
    stop.set()


def main() -> int:
    configure_from_iq_len(IQ_LEN)
    print("=== presence E2E ===")

    r = run([str(PRES / "run_presence.sh")])
    for needle in (
        "train",
        "calibrate-live",
        "live",
        "gui",
        "web",
        "test-web",
        "test-e2e",
        "capture",
        "status",
    ):
        if needle not in r.stdout:
            fail(f"help missing {needle}")
        else:
            ok(f"help has {needle}")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        csv1, m1 = td / "n1.csv", td / "m1.joblib"
        write_synthetic_csv(csv1, n_rx=1, sessions_per_class=3, packets_per_stream=80)
        r = run(
            [
                sys.executable,
                "train_object_detector.py",
                "--csv",
                str(csv1),
                "--out",
                str(m1),
                "--deploy",
                "--time-blocks",
                "2",
            ]
        )
        if r.returncode != 0:
            fail(f"N=1 train: {r.stderr[-400:]}")
        else:
            b1 = joblib.load(m1)
            ok(f"N=1 train fusion={b1.get('rx_fusion')!r}")
            det1 = make_live_detector(b1, fast=True)
            if isinstance(det1, MultiRxLiveDetector):
                fail("N=1 should be LiveDetector")
            last = None
            for k in range(b1["window_size"] + 5):
                iq = [int(x) for x in _iq(k, False).split(",")]
                last = det1.on_packet(
                    iq,
                    rssi=-45,
                    agc_gain=10,
                    fft_gain=20,
                    seq=k,
                    arrival=1000 + k * 0.08,
                )
            if last and last.get("ready"):
                line = format_line(last)
                if "P(presence)" not in line:
                    fail(f"N=1 wording: {line}")
                else:
                    ok(f"N=1 live state={last['state']}")
            else:
                fail(f"N=1 live: {last}")

        csv3, m3, cal3 = td / "n3.csv", td / "m3.joblib", td / "cal3.joblib"
        sources = write_synthetic_csv(
            csv3, n_rx=3, sessions_per_class=3, packets_per_stream=80
        )
        r = run(
            [
                sys.executable,
                "train_object_detector.py",
                "--csv",
                str(csv3),
                "--out",
                str(m3),
                "--deploy",
                "--rx-fusion",
                "concat",
                "--rx-min",
                "1",
                "--time-blocks",
                "2",
            ]
        )
        if r.returncode != 0:
            fail(f"N=3 train: {r.stderr[-400:]}")
            print("\n".join(failures))
            return 1
        b3 = joblib.load(m3)
        if b3.get("rx_fusion") != "concat" or len(b3.get("rx_sources_order") or []) != 3:
            fail(f"N=3 bundle: {b3.get('rx_fusion')} {b3.get('rx_sources_order')}")
        else:
            ok(f"N=3 concat rx_min={b3.get('rx_min')}")

        r = run(
            [
                sys.executable,
                "calibrate_site.py",
                "--model",
                str(m3),
                "--out",
                str(cal3),
                "--from-csv",
                str(csv3),
                "--fast",
                "--fpr",
                "0.05",
            ]
        )
        cal = None
        if r.returncode != 0:
            fail(f"calibrate: {r.stderr[-400:]}")
        else:
            cal = joblib.load(cal3)
            ok(
                f"calibrate windows={cal.get('n_windows')} "
                f"thr={cal.get('threshold'):+.3f}"
            )

        det = make_live_detector(b3, fast=True, calibration=cal, rx_min="auto")
        assert isinstance(det, MultiRxLiveDetector) and det.min_rx == 1

        last = feed(det, sources, b3, 1000.0)
        if last and last.get("ready") and last.get("rx_present") == 3:
            line = format_line(last)
            if "P(presence)" not in line:
                fail(f"format: {line}")
            else:
                ok("live 3/3 + wording")
        else:
            fail(f"3/3: {last}")

        det.idle_s = 0.5
        last = feed(det, sources[:1], b3, 2000.0)
        if last and last.get("ready") and last.get("rx_present") == 1:
            ok("live 1/3")
        else:
            fail(f"1/3: {last}")

        det2 = make_live_detector(b3, fast=True, rx_min="auto")
        det2.idle_s = 0.5
        last = feed(det2, sources[:2], b3, 3000.0)
        if last and last.get("ready") and last.get("rx_present") == 2:
            ok("live 2/3")
        else:
            fail(f"2/3: {last}")

        det3 = make_live_detector(b3, fast=True, rx_min="auto")
        det3.idle_s = 0.5
        last = feed(det3, [sources[0], sources[1], "10.9.9.9"], b3, 4000.0)
        if last and last.get("ready") and "10.9.9.9" in (last.get("rx_aliases") or {}):
            ok(f"hotplug {last['rx_aliases']}")
        else:
            fail(f"hotplug: {last}")

        det_all = make_live_detector(b3, fast=True, rx_min="all")
        det_all.idle_s = 0.5
        last = feed(det_all, sources[:2], b3, 5000.0)
        if last and last.get("ready"):
            fail("rx-min all should block 2/3")
        else:
            ok("rx-min all waits on 2/3")

        if cal is not None:
            det_cal = make_live_detector(b3, fast=True, calibration=cal, rx_min="auto")
            thr = float(getattr(det_cal, "threshold", cal.get("threshold")))
            if abs(thr - float(cal["threshold"])) > 1e-6:
                fail(f"cal thr not applied: live={thr} cal={cal['threshold']}")
            else:
                ok(f"cal threshold applied thr={thr:+.3f}")

        pmodel = PRES / "models" / "_e2e_tmp.joblib"
        pcal = PRES / "models" / "_e2e_cal_tmp.joblib"
        try:
            shutil.copy(m3, pmodel)
            if cal3.is_file():
                shutil.copy(cal3, pcal)
            ok("presence models round-trip")
        finally:
            pmodel.unlink(missing_ok=True)
            pcal.unlink(missing_ok=True)

        _check_web_stack(b3, sources, cal)

    print("=== unit suites ===")
    r = run([sys.executable, "test_multirx_fusion.py"])
    if r.returncode != 0 or "ALL PASSED" not in r.stdout:
        fail("test_multirx_fusion failed")
    else:
        ok("test_multirx_fusion ALL PASSED")

    r = run([str(PRES / "run_presence.sh"), "test-web"])
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        fail(f"test-web failed rc={r.returncode}: {out[-500:]}")
    elif "Ran " in out and "OK" in out:
        ok("run_presence.sh test-web OK")
    else:
        fail(f"test-web output unexpected: {out[-500:]}")

    print()
    if failures:
        print("E2E FAILURES:")
        for f in failures:
            print(" ", f)
        return 1
    print("E2E PIPELINE PASSED (train → cal → live 1..N → web → suites)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
