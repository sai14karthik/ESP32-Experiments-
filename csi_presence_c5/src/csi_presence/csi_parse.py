"""Parse esp-csi style CSI_DATA serial lines."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# CSI_DATA,<fields...>,\"[i,q,i,q,...]\"
_CSI_RE = re.compile(
    r'^CSI_DATA,(?P<meta>.+),"(?P<iq>\[[^\]]*\])"\s*$'
)


@dataclass
class CsiSample:
    raw_line: str
    rssi: Optional[float]
    iq: list[int]
    host_ts: float  # set by collector

    @property
    def amplitude(self) -> list[float]:
        out: list[float] = []
        iq = self.iq
        for i in range(0, len(iq) - 1, 2):
            out.append((iq[i] ** 2 + iq[i + 1] ** 2) ** 0.5)
        return out


def parse_csi_line(line: str, host_ts: float) -> Optional[CsiSample]:
    line = line.strip()
    if not line.startswith("CSI_DATA,"):
        return None
    m = _CSI_RE.match(line)
    if not m:
        # Fallback: last quoted bracket list
        start = line.rfind('"[')
        end = line.rfind(']"')
        if start < 0 or end < start:
            return None
        iq_str = line[start + 1 : end + 1]
        meta = line[len("CSI_DATA,") : start].rstrip(",")
    else:
        meta = m.group("meta")
        iq_str = m.group("iq")

    try:
        nums = [int(x.strip()) for x in iq_str.strip("[]").split(",") if x.strip()]
    except ValueError:
        return None
    if len(nums) < 4:
        return None

    rssi: Optional[float] = None
    parts = meta.split(",")
    # Common layouts put rssi early; try a few slots safely
    for idx in (2, 3, 1):
        if idx < len(parts):
            try:
                rssi = float(parts[idx])
                break
            except ValueError:
                continue

    return CsiSample(raw_line=line, rssi=rssi, iq=nums, host_ts=host_ts)
