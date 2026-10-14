"""Blocking CSI_DATA reader from USB serial."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Optional

import serial

from csi_presence.csi_parse import CsiSample, parse_csi_line


class CsiSerialReader:
    def __init__(self, port: str, baud: int = 115200, timeout: float = 0.2):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._ser: Optional[serial.Serial] = None

    def open(self) -> None:
        self._ser = serial.Serial(self.port, self.baud, timeout=self.timeout)

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None

    def __enter__(self) -> CsiSerialReader:
        self.open()
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def iter_samples(self) -> Iterator[CsiSample]:
        if not self._ser:
            raise RuntimeError("serial not open")
        buf = ""
        while True:
            chunk = self._ser.read(4096)
            if chunk:
                buf += chunk.decode("utf-8", errors="ignore")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    sample = parse_csi_line(line, time.time())
                    if sample is not None:
                        yield sample
            else:
                time.sleep(0.001)
