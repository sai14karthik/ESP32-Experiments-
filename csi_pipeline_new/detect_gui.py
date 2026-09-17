#!/usr/bin/env python3
"""PyQt live presence window (EMPTY vs PRESENCE).

Supports USB serial or multi-RX TCP (:9055) for fused models.

  ./run_detect.sh --gui --fast
  ./run_presence.sh gui
  python detect_live.py --gui --listen-tcp 9055 --fast
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
    MultiRxLiveDetector,
    build_live_arg_parser,
    find_port,
    iter_csi_from_file,
    iter_csi_from_serial,
    iter_csi_from_tcp,
    load_bundle_and_calibration,
    make_live_detector,
    print_startup_banner,
)

HISTORY_SECONDS = 90.0
EMPTY_BG = "#1b3a2f"
PRESENCE_BG = "#4a1c1c"
EMPTY_FG = "#7dffa3"
PRESENCE_FG = "#ff8a8a"


class DetectWorker(QThread):
    """Background CSI reader → detector → UI signals."""

    update = pyqtSignal(dict)
    status = pyqtSignal(str)
    finished_ok = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(
        self,
        detector,
        *,
        port: str | None,
        baud: int,
        from_file: Path | None,
        listen_tcp: int | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.detector = detector
        self.port = port
        self.baud = baud
        self.from_file = from_file
        self.listen_tcp = listen_tcp
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
            "source_id": meta.get("source_id"),
            "pkt_s": round(self._pkt_rate(), 1),
            "t": time.monotonic(),
        }
        self.update.emit(payload)

    def _handle(self, iq: list[int], meta: dict, *, source_id: str | None = None) -> None:
        kwargs = dict(
            rssi=float(meta.get("rssi") or 0.0),
            agc_gain=float(meta.get("agc_gain") or 0.0),
            fft_gain=float(meta.get("fft_gain") or 0.0),
            seq=meta.get("seq"),
        )
        if isinstance(self.detector, MultiRxLiveDetector):
            result = self.detector.on_packet(
                iq, source_id=source_id or meta.get("source_id"), **kwargs
            )
        else:
            result = self.detector.on_packet(iq, **kwargs)
        rate = self._pkt_rate()
        if result is None:
            return
        result = {**result, "pkt_s": round(rate, 1)}
        self._emit_result(result, meta)

    def run(self) -> None:
        try:
            if self.from_file is not None:
                self.status.emit(f"replaying {self.from_file}")
                for iq, meta in iter_csi_from_file(self.from_file, delay_s=0.05):
                    if self._stop:
                        break
                    self._handle(iq, meta)
                self.finished_ok.emit()
                return

            if self.listen_tcp is not None:
                order = getattr(self.detector, "rx_sources_order", None) or []
                self.status.emit(
                    f"tcp :{self.listen_tcp}  N={len(order) or '?'}  "
                    f"({', '.join(order) if order else 'any'})"
                )
                try:
                    for source_id, iq, meta in iter_csi_from_tcp(self.listen_tcp):
                        if self._stop:
                            break
                        self._handle(iq, meta, source_id=source_id)
                except OSError as exc:
                    raise RuntimeError(
                        f"TCP :{self.listen_tcp} failed: {exc}. "
                        "Stop ./run_multi_ingest.sh / terminal live first."
                    ) from exc
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
                self._handle(iq, meta)
            self.finished_ok.emit()
        except Exception as exc:  # noqa: BLE001 — surface to UI
            self.failed.emit(str(exc))


class PresenceWindow(QMainWindow):
    def __init__(
        self,
        detector,
        *,
        port: str | None,
        baud: int,
        from_file: Path | None,
        listen_tcp: int | None,
        calibrated: bool,
        model_name: str,
        rx_label: str = "",
    ) -> None:
        super().__init__()
        self.detector = detector
        self._history_t: deque[float] = deque()
        self._history_p: deque[float] = deque()
        self._t0 = time.monotonic()
        self._last_state: str | None = None

        title = "CSI presence"
        if rx_label:
            title = f"CSI presence — {rx_label}"
        self.setWindowTitle(title)
        self.resize(780, 560)

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
        self.p_label = QLabel("P(presence) = —")
        self.p_label.setFont(QFont("", 16))
        row.addWidget(self.p_label)
        row.addStretch(1)
        self.rx_label_w = QLabel(rx_label or "RX —")
        self.rx_label_w.setFont(QFont("", 14))
        row.addWidget(self.rx_label_w)
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
        self.plot.setLabel("left", "P(presence)")
        self.plot.setLabel("bottom", "seconds")
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
        self.status_label = QLabel(f"{model_name}  ·  {cal_txt}  ·  connecting…")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self._apply_state_style("waiting")

        self.worker = DetectWorker(
            detector,
            port=port,
            baud=baud,
            from_file=from_file,
            listen_tcp=listen_tcp,
            parent=self,
        )
        self.worker.update.connect(self.on_update)
        self.worker.status.connect(self.on_status)
        self.worker.failed.connect(self.on_failed)
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.start()

    def _apply_state_style(self, state: str) -> None:
        # Accept legacy "object" from older payloads.
        if state in ("presence", "object"):
            bg, fg = PRESENCE_BG, PRESENCE_FG
            text = "PRESENCE"
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
        if payload.get("rx_present") is not None:
            bits.append(f"rx={payload['rx_present']}/{payload.get('rx_total', '?')}")
            self.rx_label_w.setText(
                f"RX {payload['rx_present']}/{payload.get('rx_total', '?')}"
            )

        if not ready:
            # Style first — it sets the label to WAITING — then overwrite with
            # buffer / multi-RX progress so the user sees fill status.
            self._apply_state_style("waiting")
            if "rx_ready" in payload:
                self.state_label.setText(
                    f"RX {payload.get('rx_ready', 0)}/{payload.get('rx_need', '?')}"
                )
            else:
                buffered = payload.get("buffered", 0)
                need = payload.get("need", self.detector.window_size)
                self.state_label.setText(f"BUFFER {buffered}/{need}")
            self.p_label.setText("P(presence) = —")
            self.bar.setValue(0)
            self.on_status("waiting  ·  " + "  ".join(bits))
            return

        state = payload.get("state", "empty")
        p = float(payload.get("p_presence", payload.get("p_object", 0.0)))
        thr = float(payload.get("threshold", self.detector.threshold))
        self._apply_state_style(state)
        self.p_label.setText(f"P(presence) = {p:.3f}")
        self.thr_label.setText(f"threshold = {thr:.3f}")
        self.bar.setValue(int(round(max(0.0, min(1.0, p)) * 1000)))

        t = float(payload.get("t", time.monotonic())) - self._t0
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
    argv = [a for a in argv if a != "--gui"]

    p = build_live_arg_parser(include_terminal_flags=False)
    args, _unknown = p.parse_known_args(argv)

    bundle, calibration, cal_path = load_bundle_and_calibration(
        args.model,
        calibration_path=args.calibration,
        no_calibration=args.no_calibration,
    )
    if (
        bundle.get("rx_fusion") == "concat"
        and args.listen_tcp is None
        and not args.port
        and not args.from_file
    ):
        args.listen_tcp = 9055
        print("auto --listen-tcp 9055 (fused multi-RX model)", file=sys.stderr)

    if (
        args.listen_tcp is not None
        and bundle.get("rx_fusion") != "concat"
        and not args.from_file
    ):
        sys.exit(
            "Refusing --listen-tcp with a non-fused (single-RX) model.\n"
            "  Fix: cd ../presence_detection && ./run_presence.sh train && "
            "./run_presence.sh calibrate-live"
        )

    detector = make_live_detector(
        bundle,
        threshold=args.threshold,
        fast=args.fast,
        calibration=calibration,
        rx_min=getattr(args, "rx_min", "all"),
    )
    print_startup_banner(
        bundle,
        detector,
        calibration=calibration,
        cal_path=cal_path,
        fast=args.fast,
        stop_hint=(
            "Close the window to stop. Stop ingest first if using :9055."
            if args.listen_tcp is not None
            else "Close the window to stop. Do not run idf.py monitor on the same port."
        ),
    )

    port = args.port
    if args.from_file is None and args.listen_tcp is None and port is None:
        port = find_port()
        print(f"serial: {port} @ {args.baud}", file=sys.stderr)

    order = getattr(detector, "rx_sources_order", None) or []
    rx_label = f"N={len(order)}" if order else ""

    app = QApplication(sys.argv)
    app.setApplicationName("CSI presence")
    window = PresenceWindow(
        detector,
        port=port,
        baud=args.baud,
        from_file=args.from_file,
        listen_tcp=args.listen_tcp,
        calibrated=calibration is not None,
        model_name=f"{bundle.get('model_type', '?')} v{bundle.get('feature_version', '?')}",
        rx_label=rx_label,
    )
    window.show()
    raise SystemExit(app.exec_())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
