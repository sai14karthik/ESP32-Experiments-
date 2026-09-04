#!/usr/bin/env python3
"""PyQt live presence window for empty vs object detection.

Reuses LiveDetector and the serial helpers from detect_live.py. Launch with:

  ./run_detect.sh --gui
  ./run_detect.sh --gui --fast
  python detect_live.py --gui --fast
"""

from __future__ import annotations

import sys
import time
from collections import deque
from pathlib import Path

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPalette
from PyQt5.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

import pyqtgraph as pg

from detect_live import (
    LiveDetector,
    build_live_arg_parser,
    find_port,
    iter_csi_from_file,
    iter_csi_from_serial,
    load_bundle_and_calibration,
    print_startup_banner,
)

HISTORY_SECONDS = 90.0
EMPTY_BG = "#1b3a2f"
OBJECT_BG = "#4a1c1c"
EMPTY_FG = "#7dffa3"
OBJECT_FG = "#ff8a8a"


class DetectWorker(QThread):
    """Background CSI reader → LiveDetector → UI signals."""

    update = pyqtSignal(dict)
    status = pyqtSignal(str)
    finished_ok = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(
        self,
        detector: LiveDetector,
        *,
        port: str | None,
        baud: int,
        from_file: Path | None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.detector = detector
        self.port = port
        self.baud = baud
        self.from_file = from_file
        self._stop = False
        self._pkt_times: deque[float] = deque()

    def stop(self) -> None:
        self._stop = True

    def _should_stop(self) -> bool:
        return self._stop

    def _on_status(self, msg: str) -> None:
        self.status.emit(msg)

    def _pkt_rate(self) -> float:
        now = time.monotonic()
        self._pkt_times.append(now)
        while self._pkt_times and now - self._pkt_times[0] > 5.0:
            self._pkt_times.popleft()
        if len(self._pkt_times) < 2:
            return 0.0
        span = self._pkt_times[-1] - self._pkt_times[0]
        return (len(self._pkt_times) - 1) / span if span > 0 else 0.0

    def _emit_result(self, result: dict, meta: dict) -> None:
        payload = {
            **result,
            "seq": meta.get("seq"),
            "rssi": meta.get("rssi"),
            "pkt_s": round(self._pkt_rate(), 1),
            "t": time.monotonic(),
        }
        self.update.emit(payload)

    def run(self) -> None:
        try:
            if self.from_file is not None:
                self.status.emit(f"replaying {self.from_file}")
                for iq, meta in iter_csi_from_file(self.from_file, delay_s=0.05):
                    if self._stop:
                        break
                    result = self.detector.on_packet(
                        iq,
                        rssi=float(meta.get("rssi") or 0.0),
                        agc_gain=float(meta.get("agc_gain") or 0.0),
                        fft_gain=float(meta.get("fft_gain") or 0.0),
                        seq=meta.get("seq"),
                    )
                    self._pkt_rate()
                    if result is not None:
                        self._emit_result(result, meta)
                self.finished_ok.emit()
                return

            port = self.port
            if port is None:
                raise RuntimeError("No serial port configured")
            self.status.emit(f"serial: {port} @ {self.baud}")
            for iq, meta in iter_csi_from_serial(
                port,
                self.baud,
                should_stop=self._should_stop,
                on_status=self._on_status,
            ):
                if self._stop:
                    break
                result = self.detector.on_packet(
                    iq,
                    rssi=float(meta.get("rssi") or 0.0),
                    agc_gain=float(meta.get("agc_gain") or 0.0),
                    fft_gain=float(meta.get("fft_gain") or 0.0),
                    seq=meta.get("seq"),
                )
                rate = self._pkt_rate()
                if result is None:
                    # Still update pkt/s occasionally via status path in wait messages.
                    continue
                result = {**result, "pkt_s": round(rate, 1)}
                self._emit_result(result, meta)
            self.finished_ok.emit()
        except Exception as exc:  # noqa: BLE001 — surface to UI
            self.failed.emit(str(exc))


class PresenceWindow(QMainWindow):
    def __init__(
        self,
        detector: LiveDetector,
        *,
        port: str | None,
        baud: int,
        from_file: Path | None,
        calibrated: bool,
        model_name: str,
    ) -> None:
        super().__init__()
        self.detector = detector
        self._history_t: deque[float] = deque()
        self._history_p: deque[float] = deque()
        self._t0 = time.monotonic()
        self._last_state: str | None = None

        self.setWindowTitle("CSI presence")
        self.resize(720, 520)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        self.state_label = QLabel("WAITING")
        self.state_label.setAlignment(Qt.AlignCenter)
        font = QFont()
        font.setPointSize(48)
        font.setBold(True)
        self.state_label.setFont(font)
        self.state_label.setMinimumHeight(120)
        layout.addWidget(self.state_label)

        row = QHBoxLayout()
        self.p_label = QLabel("P(object) = —")
        self.p_label.setFont(QFont("", 16))
        row.addWidget(self.p_label)
        row.addStretch(1)
        self.thr_label = QLabel(f"threshold = {detector.threshold:.3f}")
        self.thr_label.setFont(QFont("", 14))
        row.addWidget(self.thr_label)
        layout.addLayout(row)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setMinimumHeight(22)
        layout.addWidget(self.bar)

        pg.setConfigOptions(antialias=True, foreground="#ddd", background="#121212")
        self.plot = pg.PlotWidget()
        self.plot.setLabel("left", "P(object)")
        self.plot.setLabel("bottom", "seconds")
        # Lock Y to probability space. Auto-range on a flat P≈1.0 series drifts
        # into nonsense ranges (e.g. -1.6..-0.6) and hides the curve off-screen.
        self.plot.setYRange(0.0, 1.0, padding=0.0)
        self.plot.enableAutoRange(axis="y", enable=False)
        self.plot.getViewBox().setLimits(yMin=-0.02, yMax=1.02)
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.curve = self.plot.plot(pen=pg.mkPen("#5ec8ff", width=2))
        thr = float(detector.threshold)
        self._thr_line = None
        if 0.0 <= thr <= 1.0:
            self._thr_line = self.plot.addLine(
                y=thr, pen=pg.mkPen("#f0c14a", width=2, style=Qt.DashLine)
            )
        layout.addWidget(self.plot, stretch=1)

        cal_txt = "calibrated" if calibrated else "NOT calibrated"
        self.status_label = QLabel(
            f"{model_name}  ·  {cal_txt}  ·  connecting…"
        )
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self._apply_state_style("waiting")

        self.worker = DetectWorker(
            detector, port=port, baud=baud, from_file=from_file, parent=self
        )
        self.worker.update.connect(self.on_update)
        self.worker.status.connect(self.on_status)
        self.worker.failed.connect(self.on_failed)
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.start()

    def _apply_state_style(self, state: str) -> None:
        if state == "object":
            bg, fg = OBJECT_BG, OBJECT_FG
            text = "OBJECT"
        elif state == "empty":
            bg, fg = EMPTY_BG, EMPTY_FG
            text = "EMPTY"
        else:
            bg, fg = "#222222", "#cccccc"
            text = "WAITING"
        self.state_label.setText(text)
        self.state_label.setStyleSheet(
            f"background-color: {bg}; color: {fg}; border-radius: 12px; padding: 16px;"
        )
        pal = self.bar.palette()
        pal.setColor(QPalette.Highlight, QColor(fg))
        self.bar.setPalette(pal)

    def on_status(self, msg: str) -> None:
        base = self.status_label.text().split("  ·  ")[0:2]
        prefix = "  ·  ".join(base) if len(base) >= 2 else self.status_label.text()
        # Keep model/calibration prefix when possible.
        if "  ·  " in self.status_label.text():
            parts = self.status_label.text().split("  ·  ")
            self.status_label.setText(f"{parts[0]}  ·  {parts[1]}  ·  {msg}")
        else:
            self.status_label.setText(msg)

    def on_update(self, payload: dict) -> None:
        ready = payload.get("ready", False)
        pkt_s = payload.get("pkt_s", 0.0)
        rssi = payload.get("rssi")
        seq = payload.get("seq")
        bits = [f"{pkt_s:.1f} pkt/s"]
        if rssi is not None:
            bits.append(f"rssi={rssi}")
        if seq is not None:
            bits.append(f"seq={seq}")

        if not ready:
            buffered = payload.get("buffered", 0)
            need = payload.get("need", self.detector.window_size)
            self.state_label.setText(f"BUFFER {buffered}/{need}")
            self._apply_state_style("waiting")
            self.p_label.setText("P(object) = —")
            self.bar.setValue(0)
            self.on_status(f"buffering {buffered}/{need}  ·  " + "  ".join(bits))
            return

        state = payload.get("state", "empty")
        p = float(payload.get("p_object", 0.0))
        thr = float(payload.get("threshold", self.detector.threshold))
        self._apply_state_style(state)
        self.p_label.setText(f"P(object) = {p:.3f}")
        self.thr_label.setText(f"threshold = {thr:.3f}")
        self.bar.setValue(int(round(max(0.0, min(1.0, p)) * 1000)))

        t = float(payload.get("t", time.monotonic())) - self._t0
        # Clamp for display — training score_kind is predict_proba, but keep the
        # curve inside the locked [0, 1] view even if a bad payload arrives.
        p_plot = float(max(0.0, min(1.0, p)))
        self._history_t.append(t)
        self._history_p.append(p_plot)
        while self._history_t and t - self._history_t[0] > HISTORY_SECONDS:
            self._history_t.popleft()
            self._history_p.popleft()
        self.curve.setData(list(self._history_t), list(self._history_p))
        if self._history_t:
            left = max(0.0, self._history_t[-1] - HISTORY_SECONDS)
            self.plot.setXRange(left, max(left + 10.0, self._history_t[-1]), padding=0.02)
        # Re-assert after setData — some pyqtgraph builds re-enable Y auto-range.
        self.plot.setYRange(0.0, 1.0, padding=0.0)
        if self._thr_line is not None and 0.0 <= thr <= 1.0:
            self._thr_line.setValue(thr)

        if state != self._last_state:
            self._last_state = state
        self.on_status("  ".join(bits))

    def on_failed(self, msg: str) -> None:
        self.state_label.setText("ERROR")
        self._apply_state_style("waiting")
        self.status_label.setText(f"error: {msg}")

    def on_finished(self) -> None:
        self.on_status("stream ended")

    def closeEvent(self, event) -> None:  # noqa: N802
        self.worker.stop()
        self.worker.wait(3000)
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Allow being called via ``detect_live.py --gui`` with leftover flags.
    argv = [a for a in argv if a != "--gui"]

    p = build_live_arg_parser(include_terminal_flags=False)
    # Ignore terminal-only flags (--quiet/--json/--eval) so mixed CLI still works.
    args, _unknown = p.parse_known_args(argv)

    bundle, calibration, cal_path = load_bundle_and_calibration(
        args.model,
        calibration_path=args.calibration,
        no_calibration=args.no_calibration,
    )
    detector = LiveDetector(
        bundle, threshold=args.threshold, fast=args.fast, calibration=calibration
    )
    print_startup_banner(
        bundle,
        detector,
        calibration=calibration,
        cal_path=cal_path,
        fast=args.fast,
        stop_hint="Close the window to stop. Do not run idf.py monitor on the same port.",
    )

    port = args.port
    if args.from_file is None and port is None:
        port = find_port()
        print(f"serial: {port} @ {args.baud}", file=sys.stderr)

    app = QApplication(sys.argv)
    app.setApplicationName("CSI presence")
    window = PresenceWindow(
        detector,
        port=port,
        baud=args.baud,
        from_file=args.from_file,
        calibrated=calibration is not None,
        model_name=f"{bundle.get('model_type', '?')} v{bundle.get('feature_version', '?')}",
    )
    window.show()
    raise SystemExit(app.exec_())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
