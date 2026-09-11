"""Parse CSI_DATA serial lines from multiple ESP firmwares (no DB deps).

Supported layouts (auto-detected):
  - Lab Espressif ``csi_recv_router`` (C5): seq,mac,…,comma I/Q in ``[…]``
  - XIAO ESP32-C6 (``csi/firmware``): mac,rssi,…,space I/Q in ``[…]``
  - Hernandez ESP32-CSI-Tool: role (AP/STA/PASSIVE),mac,…,space I/Q in ``[…]``

I/Q convention (all formats): interleaved **imag, real, imag, real, …**
Amplitude per subcarrier: ``hypot(imag, real)``.
"""

from __future__ import annotations

import csv
import io
import math
import re
from datetime import datetime, timezone
from typing import Any

DEFAULT_BAUD = 115200

_ROLE_TOKENS = frozenset({"AP", "STA", "PASSIVE", "ap", "sta", "passive"})
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


def _to_int(s: str) -> int | None:
    s = (s or "").strip()
    if s == "" or s.lower() == "null":
        return None
    try:
        return int(float(s)) if "." in s else int(s)
    except ValueError:
        return None


def _parse_iq_blob(data: str) -> list[int] | None:
    data = (data or "").strip()
    if data.startswith("[") and data.endswith("]"):
        data = data[1:-1]
    data = data.strip()
    if not data:
        return []
    # Prefer commas (lab); fall back to whitespace (XIAO / Hernandez).
    if "," in data:
        parts = [p.strip() for p in data.split(",") if p.strip() != ""]
    else:
        parts = data.split()
    try:
        return [int(float(p)) if "." in p else int(p) for p in parts]
    except ValueError:
        return None


def iq_to_amplitudes(iq: list[int] | tuple[int, ...]) -> list[float]:
    """Interleaved imag/real ints → per-subcarrier amplitude (pure Python).

    Odd trailing sample is ignored. Empty / single-int IQ → empty list.
    """
    if not iq or len(iq) < 2:
        return []
    n = len(iq) - (len(iq) % 2)
    out: list[float] = []
    for i in range(0, n, 2):
        out.append(math.hypot(float(iq[i]), float(iq[i + 1])))
    return out


def _base_sample(**fields: Any) -> dict[str, Any]:
    out = {
        "seq": None,
        "mac": "",
        "rssi": None,
        "rate": None,
        "noise_floor": None,
        "fft_gain": None,
        "agc_gain": None,
        "channel": None,
        "device_ts": None,
        "sig_len": None,
        "rx_format": None,
        "len": None,
        "first_word": None,
        "iq": [],
        "host_ts": datetime.now(timezone.utc),
        "format": "unknown",
    }
    out.update(fields)
    return out


def _parse_lab(row: list[str]) -> dict[str, Any] | None:
    if len(row) < 15:
        return None
    iq = _parse_iq_blob(row[14])
    if iq is None or len(iq) < 2:
        return None
    return _base_sample(
        format="lab_router",
        seq=_to_int(row[1]),
        mac=row[2].strip(),
        rssi=_to_int(row[3]),
        rate=_to_int(row[4]),
        noise_floor=_to_int(row[5]),
        fft_gain=_to_int(row[6]),
        agc_gain=_to_int(row[7]),
        channel=_to_int(row[8]),
        device_ts=_to_int(row[9]),
        sig_len=_to_int(row[10]),
        rx_format=_to_int(row[11]),
        len=_to_int(row[12]),
        first_word=_to_int(row[13]),
        iq=iq,
    )


def _parse_xiao_c6(row: list[str]) -> dict[str, Any] | None:
    """Match ``csi/firmware`` CSV:

    CSI_DATA,mac,rssi,rate,noise_floor,channel,second,bb_format,single_mpdu,
    sig_len,rx_state,timestamp_us,rx_seq,first_word_invalid,len,[imag real …]
    """
    if len(row) < 16:
        return None
    iq = _parse_iq_blob(row[15])
    if iq is None or len(iq) < 2:
        return None
    return _base_sample(
        format="xiao_c6",
        mac=row[1].strip(),
        rssi=_to_int(row[2]),
        rate=_to_int(row[3]),
        noise_floor=_to_int(row[4]),
        channel=_to_int(row[5]),
        rx_format=_to_int(row[7]),  # bb_format
        sig_len=_to_int(row[9]),
        device_ts=_to_int(row[11]),
        seq=_to_int(row[12]),  # rx_seq (firmware host counter)
        first_word=_to_int(row[13]),
        len=_to_int(row[14]),
        iq=iq,
    )


def _parse_hernandez(row: list[str]) -> dict[str, Any] | None:
    """ESP32-CSI-Tool CSV — role in field 1, I/Q blob last."""
    if len(row) < 16:
        return None
    iq = _parse_iq_blob(row[-1])
    if iq is None or len(iq) < 2:
        return None
    return _base_sample(
        format="hernandez",
        mac=row[2].strip() if len(row) > 2 else "",
        rssi=_to_int(row[3]) if len(row) > 3 else None,
        rate=_to_int(row[4]) if len(row) > 4 else None,
        noise_floor=_to_int(row[14]) if len(row) > 14 else None,
        channel=_to_int(row[16]) if len(row) > 16 else None,
        device_ts=_to_int(row[18]) if len(row) > 18 else None,
        sig_len=_to_int(row[20]) if len(row) > 20 else None,
        len=_to_int(row[-2]) if len(row) >= 2 else None,
        iq=iq,
    )


def parse_csi_line(line: str) -> dict[str, Any] | None:
    """Parse one CSI_DATA line from any supported firmware into a common dict."""
    line = (line or "").strip()
    if not line.startswith("CSI_DATA"):
        return None
    # Tolerate "CSI_DATA," or rare "CSI_DATA "
    if not line.startswith("CSI_DATA,"):
        if line.startswith("CSI_DATA "):
            line = "CSI_DATA," + line[len("CSI_DATA ") :]
        else:
            return None
    try:
        row = next(csv.reader(io.StringIO(line)))
    except csv.Error:
        return None
    if len(row) < 3:
        return None

    second = row[1].strip()
    if second in _ROLE_TOKENS:
        return _parse_hernandez(row)
    if _MAC_RE.match(second):
        sample = _parse_xiao_c6(row)
        if sample is not None:
            return sample
    sample = _parse_lab(row)
    if sample is not None:
        return sample
    iq = _parse_iq_blob(row[-1])
    if iq is None or len(iq) < 2:
        return None
    return _base_sample(
        format="generic",
        mac=second if _MAC_RE.match(second) else "",
        seq=_to_int(second),
        rssi=_to_int(row[2]) if len(row) > 2 else None,
        iq=iq,
    )
