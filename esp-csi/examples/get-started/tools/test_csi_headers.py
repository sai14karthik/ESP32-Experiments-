#!/usr/bin/env python3
"""Validate CSI CSV headers and I/Q layout against Espressif's C5/C6 spec.

Does not import the PyQt plotter (that pulls pandas/scipy). Parser logic is
mirrored from csi_data_read_parse.py and the header list is checked against
that file so they cannot drift.

  python3 test_csi_headers.py
  python3 test_csi_headers.py --live
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from io import StringIO
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARSER_SRC = (HERE / "csi_data_read_parse.py").read_text()

C5_HEADER = [
    "type", "seq", "mac", "rssi", "rate", "noise_floor", "fft_gain", "agc_gain",
    "channel", "local_timestamp", "sig_len", "rx_state", "len", "first_word", "data",
]
CLASSIC_N = 25
C5_N = 15
MAC_RE = re.compile(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


def _try_parse_csi_record(chunk: str):
    chunk = chunk.strip().strip("\x00")
    idx = chunk.find("CSI_DATA")
    if idx < 0:
        return None
    chunk = chunk[idx:]
    q = chunk.find('"[')
    if q < 0:
        return None
    end = chunk.find(']"', q)
    if end < 0:
        return None
    payload = chunk[q + 1 : end + 1]
    header = chunk[:q].rstrip(",")
    try:
        fields = next(csv.reader(StringIO(header)))
        iq = json.loads(payload)
    except (StopIteration, json.JSONDecodeError, ValueError):
        return None
    if len(fields) not in (CLASSIC_N - 1, C5_N - 1, CLASSIC_N, C5_N):
        return None
    if len(fields) in (CLASSIC_N - 1, C5_N - 1):
        fields = list(fields) + [payload]
    try:
        csi_len = int(fields[-3])
    except (TypeError, ValueError, IndexError):
        return None
    if csi_len != len(iq) or csi_len < 2:
        return None
    return fields, iq


def _pop_csi_records(buf: str):
    out = []
    while True:
        start = buf.find("CSI_DATA")
        if start < 0:
            return out, ""
        nxt = buf.find("CSI_DATA", start + 8)
        piece = buf[start:] if nxt < 0 else buf[start:nxt]
        if '"[' in piece and ']"' in piece:
            parsed = _try_parse_csi_record(piece)
            if parsed:
                out.append(parsed)
            buf = "" if nxt < 0 else buf[nxt:]
            continue
        if nxt < 0:
            return out, buf[start:]
        buf = buf[nxt:]


def _c5_line(seq=7, n=256):
    iq = [0, 0] * 6 + [-6, -13, -6, -14] + [i % 17 - 8 for i in range(n - 16)]
    body = ",".join(str(x) for x in iq)
    return (
        f'CSI_DATA,{seq},1a:00:00:00:00:00,-23,11,-96,32,4,11,372852,47,0,{n},0,"[{body}]"'
    )


def _classic_line(n=128):
    iq = [67, 48, 4, 0] + [i % 11 - 5 for i in range(n - 4)]
    body = ",".join(str(x) for x in iq)
    return (
        "CSI_DATA,0,94:d9:b3:80:8c:81,-30,11,1,6,1,0,1,0,1,0,0,-93,0,13,2,"
        f'2751923,0,67,0,{n},1,"[{body}]"'
    )


def test_header_in_plotter_source():
    assert "DATA_COLUMNS_NAMES_C5C6" in PARSER_SRC
    for name in C5_HEADER:
        assert f"'{name}'" in PARSER_SRC, name
    c5_block = PARSER_SRC.split("DATA_COLUMNS_NAMES_C5C6", 1)[1].split("DATA_COLUMNS_NAMES", 1)[0]
    assert "'seq'" in c5_block and "'id'" not in c5_block
    print("PASS  csi_data_read_parse.py C5 header uses seq (not id) and Espressif names")
    print("     ", ",".join(C5_HEADER))


def test_c5_sample():
    parsed = _try_parse_csi_record(_c5_line())
    assert parsed, "C5 sample did not parse"
    fields, iq = parsed
    row = dict(zip(C5_HEADER, fields))
    assert row["type"] == "CSI_DATA"
    assert row["seq"] == "7"
    assert row["mac"] == "1a:00:00:00:00:00"
    assert row["rssi"] == "-23"
    assert row["noise_floor"] == "-96"
    assert row["fft_gain"] == "32"
    assert row["agc_gain"] == "4"
    assert int(row["len"]) == 256 == len(iq)
    assert len(iq) % 2 == 0
    imag0, real0 = iq[0], iq[1]
    assert complex(real0, imag0) == 0j
    print("PASS  C5 sample: 15 fields, len==256, I/Q is imag then real")


def test_classic_sample():
    parsed = _try_parse_csi_record(_classic_line())
    assert parsed
    fields, iq = parsed
    assert len(fields) == 25
    assert fields[0] == "CSI_DATA"
    assert int(fields[-3]) == len(iq) == 128
    print("PASS  classic ESP32 sample: 25 fields, len==128")


def test_glued_uart():
    recs, _rest = _pop_csi_records(_c5_line() + _classic_line() + "\n")
    assert len(recs) == 2, f"got {len(recs)}"
    assert recs[0][0][1] == "7"
    assert recs[1][0][1] == "0"
    print("PASS  glued UART line splits into 2 records")


def test_live(port: str, seconds: float = 8.0):
    import serial

    print(f"LIVE  {port} for {seconds:.0f}s")
    ser = serial.Serial(port, 115200, timeout=0.3)
    deadline = time.time() + seconds
    buf = ""
    ok = 0
    header_seen = False
    first = None
    while time.time() < deadline:
        chunk = ser.read(4096)
        if chunk:
            buf += chunk.decode("utf-8", errors="ignore")
        if "type,seq,mac,rssi,rate,noise_floor,fft_gain,agc_gain" in buf:
            header_seen = True
        recs, buf = _pop_csi_records(buf)
        for fields, iq in recs:
            assert len(fields) == 15, f"expected 15 C5 fields, got {len(fields)}"
            row = dict(zip(C5_HEADER, fields))
            assert MAC_RE.match(row["mac"]), row["mac"]
            assert int(row["len"]) == len(iq)
            assert len(iq) % 2 == 0
            rssi = int(row["rssi"])
            assert -127 <= rssi <= 0
            if first is None:
                first = (row, iq)
            ok += 1
        if len(buf) > 50000:
            buf = buf[-10000:]
    ser.close()

    print(f"LIVE  complete_frames={ok} header_printed={header_seen}")
    assert ok >= 5, f"only {ok} frames — C5 not streaming CSI?"
    row, iq = first
    n_sc = len(iq) // 2
    amps = [abs(complex(iq[2 * k + 1], iq[2 * k])) for k in range(n_sc)]
    print("LIVE  first frame:")
    for k in ("seq", "mac", "rssi", "rate", "noise_floor", "fft_gain", "agc_gain",
              "channel", "sig_len", "rx_state", "len", "first_word"):
        print(f"      {k}={row[k]}")
    print(f"      subcarriers={n_sc}  I/Q[0] imag={iq[0]} real={iq[1]}")
    print(f"      |H| peak={max(amps):.1f} mean={sum(amps)/len(amps):.1f}")
    print("PASS  live C5 stream: 15-field header + imag/real pairs")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true")
    p.add_argument("-p", "--port", default="/dev/cu.usbmodem101")
    args = p.parse_args()
    test_header_in_plotter_source()
    test_c5_sample()
    test_classic_sample()
    test_glued_uart()
    if args.live:
        test_live(args.port)
    print("\nAll CSI header checks passed.")


if __name__ == "__main__":
    main()
