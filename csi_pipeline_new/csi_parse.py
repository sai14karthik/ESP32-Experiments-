"""Parse CSI_DATA serial lines (no DB or serial deps)."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any

DEFAULT_BAUD = 115200


def parse_csi_line(line: str) -> dict[str, Any] | None:
    """Parse one CSI_DATA CSV line into fields + iq list."""
    if not line.startswith("CSI_DATA,"):
        return None
    try:
        row = next(csv.reader(io.StringIO(line)))
    except csv.Error:
        return None
    if len(row) < 15:
        return None

    (
        _type,
        seq,
        mac,
        rssi,
        rate,
        noise_floor,
        fft_gain,
        agc_gain,
        channel,
        local_timestamp,
        sig_len,
        rx_format,
        length,
        first_word,
        data,
    ) = row[:15]

    data = data.strip()
    if data.startswith("[") and data.endswith("]"):
        data = data[1:-1]
    if not data.strip():
        iq: list[int] = []
    else:
        try:
            iq = [int(x.strip()) for x in data.split(",") if x.strip() != ""]
        except ValueError:
            return None

    def to_int(s: str) -> int | None:
        s = s.strip()
        if s == "" or s.lower() == "null":
            return None
        try:
            return int(s)
        except ValueError:
            return None

    return {
        "seq": to_int(seq),
        "mac": mac.strip(),
        "rssi": to_int(rssi),
        "rate": to_int(rate),
        "noise_floor": to_int(noise_floor),
        "fft_gain": to_int(fft_gain),
        "agc_gain": to_int(agc_gain),
        "channel": to_int(channel),
        "device_ts": to_int(local_timestamp),
        "sig_len": to_int(sig_len),
        "rx_format": to_int(rx_format),
        "len": to_int(length),
        "first_word": to_int(first_word),
        "iq": iq,
        "host_ts": datetime.now(timezone.utc),
    }
