#!/usr/bin/env python3
"""End-to-end presence pipeline proof (synthetic; no hardware).

  cd csi_pipeline_new && source ./uv_common.sh && uv_csi test_presence_e2e.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
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


def main() -> int:
    configure_from_iq_len(IQ_LEN)
    print("=== presence E2E ===")

    r = run([str(PRES / "run_presence.sh"), "help"])
    for needle in ("train", "calibrate-live", "live", "gui", "capture", "status"):
        if needle not in r.stdout:
            fail(f"help missing {needle}")
        else:
            ok(f"help has {needle}")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        # N=1
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
                ok(f"N=1 live state={last['state']}")
            else:
                fail(f"N=1 live: {last}")

        # N=3
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
            ok(f"calibrate windows={cal.get('n_windows')} thr={cal.get('threshold'):+.3f}")

        det = make_live_detector(b3, fast=True, calibration=cal, rx_min="auto")
        assert isinstance(det, MultiRxLiveDetector) and det.min_rx == 1

        last = feed(det, sources, b3, 1000.0)
        if last and last.get("ready") and last.get("rx_present") == 3:
            line = format_line(last)
            if "P(presence)" not in line:
                fail(f"format: {line}")
            else:
                ok(f"live 3/3 + wording")
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

        pmodel = PRES / "models" / "_e2e_tmp.joblib"
        try:
            shutil.copy(m3, pmodel)
            ok("presence models round-trip")
        finally:
            pmodel.unlink(missing_ok=True)

    r = run([sys.executable, "test_multirx_fusion.py"])
    if r.returncode != 0 or "ALL PASSED" not in r.stdout:
        fail("test_multirx_fusion failed")
    else:
        ok("test_multirx_fusion ALL PASSED")

    print()
    if failures:
        print("E2E FAILURES:")
        for f in failures:
            print(" ", f)
        return 1
    print("E2E PIPELINE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
