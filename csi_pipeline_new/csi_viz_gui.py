#!/usr/bin/env python3
"""Live CSI visualizer for a USB-connected ESP (any supported CSI_DATA layout).

Auto-detects lab C5 ``csi_recv_router``, XIAO C6, and Hernandez ESP32-CSI-Tool
lines via ``csi_parse.parse_csi_line``.

Plug in csi_recv / csi_recv_router, then:

  ./run_csi_viz.sh
  ./run_csi_viz.sh --from-file fixtures/sample_csi_lines.csv

Do not run ingest/detect on the same serial port at the same time.
"""

from __future__ import annotations

import argparse
import glob
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np
import pyqtgraph as pg
import serial
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from csi_parse import DEFAULT_BAUD, parse_csi_line

HISTORY_SECONDS = 30.0
HEATMAP_COLS = 120
UI_EMIT_HZ = 20.0


def list_serial_ports() -> list[str]:
    return sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/cu.usbserial*"))


def iq_amplitudes(iq: list[int]) -> np.ndarray:
    """Interleaved I/Q ints → per-subcarrier amplitude."""
    a = np.asarray(iq, dtype=np.float64)
    if a.size < 2:
        return np.empty(0, dtype=np.float64)
    i = a[0::2]
    q = a[1::2]
    n = min(i.size, q.size)
    return np.sqrt(i[:n] ** 2 + q[:n] ** 2)


class CsiWorker(QThread):
    """Background serial/file reader → UI packets (rate-limited)."""

    packet = pyqtSignal(dict)
    status = pyqtSignal(str)
    finished_ok = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(
        self,
        *,
        port: str | None,
        baud: int,
        from_file: Path | None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.port = port
        self.baud = baud
        self.from_file = from_file
        self._stop = False
        self._pkt_times: deque[float] = deque()

    def stop(self) -> None:
        self._stop = True

    def _pkt_rate(self) -> float:
        now = time.monotonic()
        self._pkt_times.append(now)
        while self._pkt_times and now - self._pkt_times[0] > 5.0:
            self._pkt_times.popleft()
        if len(self._pkt_times) < 2:
            return 0.0
        span = self._pkt_times[-1] - self._pkt_times[0]
        return (len(self._pkt_times) - 1) / span if span > 0 else 0.0

    def _emit_sample(self, sample: dict, *, force: bool, last_emit: list[float]) -> None:
        now = time.monotonic()
        rate = self._pkt_rate()
        min_dt = 1.0 / UI_EMIT_HZ
        if not force and (now - last_emit[0]) < min_dt:
            return
        last_emit[0] = now
        amps = iq_amplitudes(sample["iq"])
        if amps.size == 0:
            return
        self.packet.emit(
            {
                "t": now,
                "seq": sample.get("seq"),
                "rssi": sample.get("rssi"),
                "mac": sample.get("mac"),
                "channel": sample.get("channel"),
                "mean_amp": float(np.mean(amps)),
                "amps": amps,
                "pkt_s": round(rate, 1),
                "format": sample.get("format"),
            }
        )

    def run(self) -> None:
        last_emit = [0.0]
        try:
            if self.from_file is not None:
                self.status.emit(f"replaying {self.from_file}")
                with self.from_file.open(encoding="utf-8") as f:
                    lines = f.readlines()
                # Loop short fixtures so the UI stays alive for demos.
                while not self._stop:
                    for raw in lines:
                        if self._stop:
                            break
                        sample = parse_csi_line(raw.strip())
                        if not sample or not sample.get("iq"):
                            continue
                        self._emit_sample(sample, force=False, last_emit=last_emit)
                        time.sleep(0.05)
                self.finished_ok.emit()
                return

            if not self.port:
                raise RuntimeError("No serial port selected")

            self.status.emit(f"opening {self.port} @ {self.baud}")
            with serial.Serial(self.port, self.baud, timeout=0.2) as ser:
                ser.dtr = False
                ser.rts = False
                time.sleep(0.3)
                ser.reset_input_buffer()
                self.status.emit(f"listening on {self.port} — waiting for CSI_DATA…")
                buf = ""
                last_csi = time.monotonic()
                last_warn = 0.0
                count = 0
                while not self._stop:
                    chunk = ser.read(ser.in_waiting or 1)
                    if not chunk:
                        now = time.monotonic()
                        if now - last_csi > 5.0 and now - last_warn > 5.0:
                            last_warn = now
                            self.status.emit(
                                f"waiting for CSI_DATA… ({count} packets so far)"
                            )
                        continue
                    buf += chunk.decode("utf-8", errors="replace")
                    while "\n" in buf:
                        if self._stop:
                            break
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        sample = parse_csi_line(line)
                        if not sample or not sample.get("iq"):
                            continue
                        count += 1
                        last_csi = time.monotonic()
                        if count == 1:
                            self.status.emit(
                                f"CSI OK [{sample.get('format')}]  "
                                f"mac={sample.get('mac')}  "
                                f"ch={sample.get('channel')}  "
                                f"rssi={sample.get('rssi')}"
                            )
                        self._emit_sample(
                            sample, force=(count <= 3), last_emit=last_emit
                        )
            self.finished_ok.emit()
        except Exception as exc:  # noqa: BLE001 — surface in UI
            self.failed.emit(str(exc))


class CsiVizWindow(QMainWindow):
    def __init__(self, *, port: str | None, baud: int, from_file: Path | None) -> None:
        super().__init__()
        self.setWindowTitle("CSI Live Scope")
        self.resize(1100, 720)

        self._baud = baud
        self._from_file = from_file
        self._worker: CsiWorker | None = None
        self._t0: float | None = None
        self._history_t: deque[float] = deque()
        self._history_mean: deque[float] = deque()
        self._history_rssi: deque[float] = deque()
        self._history_sc: deque[float] = deque()
        self._heat = np.zeros((1, HEATMAP_COLS), dtype=np.float32)
        self._heat_n_sc = 1
        self._heat_col = 0
        self._heat_filled = False

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        # Controls
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Port"))
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(220)
        bar.addWidget(self.port_combo)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_ports)
        bar.addWidget(self.refresh_btn)
        bar.addWidget(QLabel("Baud"))
        self.baud_spin = QSpinBox()
        self.baud_spin.setRange(9600, 3000000)
        self.baud_spin.setValue(baud)
        bar.addWidget(self.baud_spin)
        bar.addWidget(QLabel("Subcarrier"))
        self.sc_spin = QSpinBox()
        self.sc_spin.setRange(0, 512)
        self.sc_spin.setValue(10)
        bar.addWidget(self.sc_spin)
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self.toggle_start)
        bar.addWidget(self.start_btn)
        bar.addStretch(1)
        layout.addLayout(bar)

        self.status = QLabel("Select a port and press Start (or use --from-file).")
        self.status.setStyleSheet("color: #9ab; padding: 4px;")
        layout.addWidget(self.status)

        pg.setConfigOptions(antialias=True, background="#0f1419", foreground="#c8d0d8")

        # Mean amplitude + RSSI
        self.plot_amp = pg.PlotWidget(title="Mean CSI amplitude vs time")
        self.plot_amp.setLabel("left", "amplitude")
        self.plot_amp.setLabel("bottom", "seconds")
        self.plot_amp.showGrid(x=True, y=True, alpha=0.25)
        self.curve_mean = self.plot_amp.plot(pen=pg.mkPen("#5ec8ff", width=2), name="mean")
        self.curve_sc = self.plot_amp.plot(pen=pg.mkPen("#ffb454", width=1.5), name="subcarrier")
        layout.addWidget(self.plot_amp, stretch=2)

        self.plot_rssi = pg.PlotWidget(title="RSSI vs time")
        self.plot_rssi.setLabel("left", "dBm")
        self.plot_rssi.setLabel("bottom", "seconds")
        self.plot_rssi.showGrid(x=True, y=True, alpha=0.25)
        self.curve_rssi = self.plot_rssi.plot(pen=pg.mkPen("#7dffa3", width=2))
        layout.addWidget(self.plot_rssi, stretch=1)

        # Heatmap: subcarrier × time
        self.plot_heat = pg.PlotWidget(title="Amplitude heatmap (subcarrier × time)")
        self.plot_heat.setLabel("left", "subcarrier")
        self.plot_heat.setLabel("bottom", "← older    newer →")
        self.img = pg.ImageItem()
        self.plot_heat.addItem(self.img)
        # Viridis-like
        stops = [
            (0.0, (30, 30, 50)),
            (0.25, (40, 80, 140)),
            (0.5, (40, 160, 140)),
            (0.75, (200, 200, 60)),
            (1.0, (250, 250, 200)),
        ]
        self.img.setLookupTable(pg.ColorMap([s[0] for s in stops], [s[1] for s in stops]).getLookupTable())
        layout.addWidget(self.plot_heat, stretch=2)

        self.stats = QLabel("pkt/s: —   seq: —   rssi: —   subcarriers: —")
        self.stats.setFont(QFont("Menlo", 12) if sys.platform == "darwin" else QFont("monospace", 11))
        layout.addWidget(self.stats)

        self.refresh_ports()
        if port:
            idx = self.port_combo.findText(port)
            if idx >= 0:
                self.port_combo.setCurrentIndex(idx)
            else:
                self.port_combo.addItem(port)
                self.port_combo.setCurrentText(port)

        if from_file is not None:
            self.status.setText(f"File mode: {from_file} — press Start to replay")
            self.port_combo.setEnabled(False)
            self.refresh_btn.setEnabled(False)

    def refresh_ports(self) -> None:
        current = self.port_combo.currentText()
        ports = list_serial_ports()
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        if ports:
            self.port_combo.addItems(ports)
            if current in ports:
                self.port_combo.setCurrentText(current)
        else:
            self.port_combo.addItem("(no USB serial — plug in ESP)")
        self.port_combo.blockSignals(False)

    def toggle_start(self) -> None:
        if self._worker and self._worker.isRunning():
            self._stop_worker()
            return
        self._start_worker()

    def _start_worker(self) -> None:
        self._t0 = None
        self._history_t.clear()
        self._history_mean.clear()
        self._history_rssi.clear()
        self._history_sc.clear()
        self._heat = np.zeros((1, HEATMAP_COLS), dtype=np.float32)
        self._heat_n_sc = 1
        self._heat_col = 0
        self._heat_filled = False

        port = None
        if self._from_file is None:
            port = self.port_combo.currentText().strip()
            if not port or port.startswith("("):
                QMessageBox.warning(self, "CSI Scope", "No serial port available.")
                return

        self._worker = CsiWorker(
            port=port,
            baud=int(self.baud_spin.value()),
            from_file=self._from_file,
        )
        self._worker.packet.connect(self.on_packet)
        self._worker.status.connect(self.on_status)
        self._worker.failed.connect(self.on_failed)
        self._worker.finished_ok.connect(self.on_finished)
        self.start_btn.setText("Stop")
        self.port_combo.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.baud_spin.setEnabled(False)
        self._worker.start()

    def _stop_worker(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(2000)
            self._worker = None
        self.start_btn.setText("Start")
        if self._from_file is None:
            self.port_combo.setEnabled(True)
            self.refresh_btn.setEnabled(True)
        self.baud_spin.setEnabled(True)
        self.status.setText("Stopped.")

    def on_status(self, msg: str) -> None:
        self.status.setText(msg)

    def on_failed(self, msg: str) -> None:
        self._stop_worker()
        QMessageBox.critical(self, "CSI Scope", msg)
        self.status.setText(f"Error: {msg}")

    def on_finished(self) -> None:
        self._stop_worker()

    def on_packet(self, pkt: dict) -> None:
        t_abs = float(pkt["t"])
        if self._t0 is None:
            self._t0 = t_abs
        t = t_abs - self._t0

        amps = pkt["amps"]
        mean_amp = float(pkt["mean_amp"])
        rssi = pkt.get("rssi")
        sc_idx = int(self.sc_spin.value())
        sc_val = float(amps[sc_idx]) if 0 <= sc_idx < amps.size else mean_amp

        self._history_t.append(t)
        self._history_mean.append(mean_amp)
        self._history_sc.append(sc_val)
        self._history_rssi.append(float(rssi) if rssi is not None else np.nan)

        while self._history_t and (t - self._history_t[0]) > HISTORY_SECONDS:
            self._history_t.popleft()
            self._history_mean.popleft()
            self._history_sc.popleft()
            self._history_rssi.popleft()

        # Heatmap column
        n_sc = int(amps.size)
        if n_sc != self._heat_n_sc:
            self._heat_n_sc = n_sc
            self._heat = np.zeros((n_sc, HEATMAP_COLS), dtype=np.float32)
            self._heat_col = 0
            self._heat_filled = False
            self.sc_spin.setMaximum(max(0, n_sc - 1))

        col = self._heat_col % HEATMAP_COLS
        self._heat[:, col] = amps.astype(np.float32)
        self._heat_col += 1
        if self._heat_col >= HEATMAP_COLS:
            self._heat_filled = True

        # Display heatmap with newest on the right
        if self._heat_filled:
            order = (np.arange(HEATMAP_COLS) + self._heat_col) % HEATMAP_COLS
            heat_view = self._heat[:, order]
        else:
            heat_view = self._heat[:, : max(1, self._heat_col)]

        ts = np.asarray(self._history_t, dtype=np.float64)
        self.curve_mean.setData(ts, np.asarray(self._history_mean, dtype=np.float64))
        self.curve_sc.setData(ts, np.asarray(self._history_sc, dtype=np.float64))
        self.curve_rssi.setData(ts, np.asarray(self._history_rssi, dtype=np.float64))

        if len(ts) >= 2:
            left = max(0.0, ts[-1] - HISTORY_SECONDS)
            self.plot_amp.setXRange(left, max(left + 5.0, ts[-1]), padding=0.02)
            self.plot_rssi.setXRange(left, max(left + 5.0, ts[-1]), padding=0.02)

        self.img.setImage(heat_view, autoLevels=True, axes={"x": 1, "y": 0})
        self.plot_heat.setXRange(0, heat_view.shape[1], padding=0)
        self.plot_heat.setYRange(0, heat_view.shape[0], padding=0)

        self.stats.setText(
            f"[{pkt.get('format', '?')}]  "
            f"pkt/s: {pkt.get('pkt_s', '—')}   "
            f"seq: {pkt.get('seq', '—')}   "
            f"rssi: {rssi if rssi is not None else '—'}   "
            f"subcarriers: {n_sc}   "
            f"mean_amp: {mean_amp:.1f}"
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        self._stop_worker()
        super().closeEvent(event)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", help="Serial port (default: first usbmodem/usbserial)")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    p.add_argument(
        "--from-file",
        type=Path,
        help="Replay CSI_DATA lines (no hardware; loops short files)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    port = args.port
    if args.from_file is None and port is None:
        ports = list_serial_ports()
        port = ports[0] if ports else None

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = CsiVizWindow(port=port, baud=args.baud, from_file=args.from_file)
    win.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
