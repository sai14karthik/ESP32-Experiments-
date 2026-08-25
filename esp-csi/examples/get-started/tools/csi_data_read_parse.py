#!/usr/bin/env python3
# -*-coding:utf-8-*-

# SPDX-FileCopyrightText: 2021-2025 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
#

# WARNING: we don't check for Python build-time dependencies until
# check_environment() function below. If possible, avoid importing
# any external libraries here - put in external script, or import in
# their specific function instead.

import sys
import csv
import json
import argparse
import pandas as pd
import numpy as np

import serial
from os import path
from io import StringIO

from PyQt5.Qt import *
from pyqtgraph import PlotWidget
from PyQt5 import QtCore
import pyqtgraph as pg
from pyqtgraph import ScatterPlotItem
from PyQt5.QtCore import pyqtSignal, QThread
import threading
import time
from scipy.optimize import minimize
import matplotlib.pyplot as plt
from scipy.stats import linregress
import statsmodels.api as sm

# Reduce displayed waveforms to avoid display freezes
CSI_VAID_SUBCARRIER_INTERVAL = 1
csi_vaid_subcarrier_len =0

CSI_DATA_INDEX = 200  # buffer size
CSI_DATA_COLUMNS = 490
# ESP32-C5/C6 CSV (Espressif CSI guide). 15 fields. I/Q in data: imag, real, imag, real, ...
DATA_COLUMNS_NAMES_C5C6 = ['type', 'seq', 'mac', 'rssi', 'rate', 'noise_floor', 'fft_gain', 'agc_gain',
                           'channel', 'local_timestamp', 'sig_len', 'rx_state', 'len', 'first_word', 'data']
DATA_COLUMNS_NAMES = ['type', 'id', 'mac', 'rssi', 'rate', 'sig_mode', 'mcs', 'bandwidth', 'smoothing', 'not_sounding', 'aggregation', 'stbc', 'fec_coding',
                      'sgi', 'noise_floor', 'ampdu_cnt', 'channel', 'secondary_channel', 'local_timestamp', 'ant', 'sig_len', 'rx_state', 'len', 'first_word', 'data']

csi_data_array = np.zeros(
    [CSI_DATA_INDEX, CSI_DATA_COLUMNS], dtype=np.float64)
csi_data_phase = np.zeros([CSI_DATA_INDEX, CSI_DATA_COLUMNS], dtype=np.float64)
csi_data_complex = np.zeros([CSI_DATA_INDEX, CSI_DATA_COLUMNS], dtype=np.complex64)
agc_gain_data = np.zeros([CSI_DATA_INDEX], dtype=np.float64)
fft_gain_data = np.zeros([CSI_DATA_INDEX], dtype=np.float64)
fft_gains = []
agc_gains = []

class csi_data_graphical_window(QWidget):
    def __init__(self):
        super().__init__()

        self.resize(1280, 900)

        self.plotWidget_ted = PlotWidget(self)
        self.plotWidget_ted.setGeometry(QtCore.QRect(0, 0, 640, 300))
        self.plotWidget_ted.setYRange(-2*np.pi, 2*np.pi)
        self.plotWidget_ted.addLegend()
        self.plotWidget_ted.setTitle('Phase Data - Last Frame')  # 添加标题
        self.plotWidget_ted.setLabel('left', 'Phase (rad)')  # Y轴标签
        self.plotWidget_ted.setLabel('bottom', 'Subcarrier Index')  # X轴标签

        self.csi_amplitude_array = np.abs(csi_data_complex)
        self.csi_phase_array = np.angle(csi_data_complex)
        self.curve = self.plotWidget_ted.plot([], name='CSI Row Data', pen='r')

        self.plotWidget_multi_data = PlotWidget(self)
        self.plotWidget_multi_data.setGeometry(QtCore.QRect(0, 300, 1280, 300))
        self.plotWidget_multi_data.getViewBox().enableAutoRange(axis=pg.ViewBox.YAxis)
        self.plotWidget_multi_data.addLegend()
        self.plotWidget_multi_data.setTitle('Subcarrier Amplitude Data')  # 添加标题
        self.plotWidget_multi_data.setLabel('left', 'Amplitude')  # Y轴标签
        self.plotWidget_multi_data.setLabel('bottom', 'Time (Cumulative Packet Count)')  # X轴标签

        self.curve_list = []
        agc_curve = self.plotWidget_multi_data.plot(
            agc_gain_data, name='AGC Gain', pen=[255,255,0])
        fft_curve = self.plotWidget_multi_data.plot(
            fft_gain_data, name='FFT Gain', pen=[255,255,0])
        self.curve_list.append(agc_curve)
        self.curve_list.append(fft_curve)

        for i in range(CSI_DATA_COLUMNS):
            curve = self.plotWidget_multi_data.plot(
                self.csi_amplitude_array[:, i], name=str(i), pen=(255, 255, 255))
            self.curve_list.append(curve)



        self.plotWidget_phase_data = PlotWidget(self)
        self.plotWidget_phase_data.setGeometry(QtCore.QRect(0, 600, 1280, 300))
        self.plotWidget_phase_data.getViewBox().enableAutoRange(axis=pg.ViewBox.YAxis)
        self.plotWidget_phase_data.addLegend()
        self.plotWidget_multi_data.setTitle('Subcarrier Phase Data')  # 添加标题
        self.plotWidget_multi_data.setLabel('left', 'Phase (rad)')  # Y轴标签
        self.plotWidget_multi_data.setLabel('bottom', 'Time (Cumulative Packet Count)')  # X轴标签


        self.curve_phase_list = []
        for i in range(CSI_DATA_COLUMNS):
            phase_curve = self.plotWidget_phase_data.plot(
                np.angle(self.csi_amplitude_array[:, i]), name=str(i), pen=(255, 255, 255))
            self.curve_phase_list.append(phase_curve)


        # IQ 图窗口
        self.plotWidget_iq = PlotWidget(self)
        self.plotWidget_iq.setGeometry(QtCore.QRect(640, 0, 640, 300))
        self.plotWidget_iq.setLabel('left', 'Q (Imag)')
        self.plotWidget_iq.setLabel('bottom', 'I (Real)')
        self.plotWidget_iq.setTitle('IQ Plot - Last Frame')
        view_box = self.plotWidget_iq.getViewBox()
        view_box.setRange(QtCore.QRectF(-30, -30, 60, 60))  # 可以调整范围的大小，保证原点在中间

        self.plotWidget_iq.getViewBox().setAspectLocked(True)
        self.iq_scatter = ScatterPlotItem(size=6)
        self.plotWidget_iq.addItem(self.iq_scatter)

        self.iq_colors = []



        self.timer = pg.QtCore.QTimer()
        self.timer.timeout.connect(self.update_data)
        self.timer.start(100)
        self.deta_len = 0

    def update_curve_colors(self, color_list):
        self.deta_len = len(color_list)
        self.iq_colors = color_list
        self.plotWidget_ted.setXRange(0, self.deta_len//2)
        for i in range(self.deta_len):
            self.curve_list[i].setPen(color_list[i])
            self.curve_phase_list[i].setPen(color_list[i])

    def update_data(self):

        i = np.real(csi_data_complex[-1, :])
        q = np.imag(csi_data_complex[-1, :])

        points = []
        for idx in range(self.deta_len):
            if idx < len(self.iq_colors):
                color = self.iq_colors[idx]
            else:
                color = (200, 200, 200)
            points.append({'pos': (i[idx], q[idx]), 'brush': pg.mkBrush(color)})

        self.iq_scatter.setData(points)


        self.csi_amplitude_array = np.abs(csi_data_complex)
        self.csi_phase_array = np.angle(csi_data_complex)
        self.csi_row_data = self.csi_phase_array[-1, :]

        self.curve.setData(self.csi_row_data)

        self.curve_list[CSI_DATA_COLUMNS].setData(agc_gain_data)
        self.curve_list[CSI_DATA_COLUMNS+1].setData(fft_gain_data)

        for i in range(CSI_DATA_COLUMNS):
            self.curve_list[i].setData(self.csi_amplitude_array[:, i])
            self.curve_phase_list[i].setData(self.csi_phase_array[:, i])

def generate_subcarrier_colors(red_range, green_range, yellow_range, total_num,interval=1):
    colors = []
    for i in range(total_num):
        if red_range and red_range[0] <= i <= red_range[1]:
            intensity = int(255 * (i - red_range[0]) / (red_range[1] - red_range[0]))
            colors.append((intensity, 0, 0))
        elif green_range and green_range[0] <= i <= green_range[1]:
            intensity = int(255 * (i - green_range[0]) / (green_range[1] - green_range[0]))
            colors.append((0, intensity, 0))
        elif yellow_range and yellow_range[0] <= i <= yellow_range[1]:
            intensity = int(255 * (i - yellow_range[0]) / (yellow_range[1] - yellow_range[0]))
            colors.append((0, intensity, intensity))
        else:
            colors.append((200, 200, 200))

    return colors


def _try_parse_csi_record(chunk: str):
    """Parse one CSI_DATA record. Returns (fields, iq_list) or None."""
    chunk = chunk.strip().strip('\x00')
    idx = chunk.find('CSI_DATA')
    if idx < 0:
        return None
    chunk = chunk[idx:]
    q = chunk.find('"[')
    if q < 0:
        return None
    end = chunk.find(']"', q)
    if end < 0:
        return None
    payload = chunk[q + 1:end + 1]
    header = chunk[:q].rstrip(',')
    try:
        fields = next(csv.reader(StringIO(header)))
        iq = json.loads(payload)
    except (StopIteration, json.JSONDecodeError, ValueError):
        return None
    n_classic = len(DATA_COLUMNS_NAMES)
    n_c5 = len(DATA_COLUMNS_NAMES_C5C6)
    if len(fields) not in (n_classic - 1, n_c5 - 1, n_classic, n_c5):
        return None
    if len(fields) in (n_classic - 1, n_c5 - 1):
        fields = list(fields) + [payload]
    try:
        csi_len = int(fields[-3])
    except (TypeError, ValueError, IndexError):
        return None
    if csi_len != len(iq) or csi_len < 2:
        return None
    return fields, iq


def _pop_csi_records(buf: str):
    """Split a UART buffer that may contain glued CSI_DATA lines."""
    out = []
    while True:
        start = buf.find('CSI_DATA')
        if start < 0:
            return out, ''
        nxt = buf.find('CSI_DATA', start + 8)
        piece = buf[start:] if nxt < 0 else buf[start:nxt]
        if '"[' in piece and ']"' in piece:
            parsed = _try_parse_csi_record(piece)
            if parsed:
                out.append(parsed)
            buf = '' if nxt < 0 else buf[nxt:]
            continue
        if nxt < 0:
            return out, buf[start:]
        buf = buf[nxt:]


def csi_data_read_parse(port: str, csv_writer, log_file_fd,callback=None):
    global fft_gains, agc_gains
    ser = serial.Serial(port=port, baudrate=115200, bytesize=8, parity='N', stopbits=1, timeout=1)
    count = 0
    skipped = 0
    buf = ''
    if ser.isOpen():
        print('open success')
    else:
        print('open failed')
        return
    while True:
        raw = ser.readline()
        if not raw:
            continue
        strings = raw.decode('utf-8', errors='ignore').replace('\r', '')
        if 'CSI_DATA' not in strings and 'CSI_DATA' not in buf:
            log_file_fd.write(strings)
            log_file_fd.flush()
            continue

        buf += strings
        if len(buf) > 200000:
            buf = buf[-40000:]
        records, buf = _pop_csi_records(buf)
        if not records:
            skipped += 1
            if skipped <= 3 or skipped % 50 == 0:
                print('waiting for a complete CSI frame (UART busy)...', skipped)
            continue

        for csi_data, csi_raw_data in records:
            csi_data_len = int(csi_data[-3])
            # C5: indices 6/7 are fft_gain/agc_gain. Classic ESP32 those slots are mcs/bandwidth.
            if len(csi_data) == len(DATA_COLUMNS_NAMES_C5C6):
                fft_gain = int(csi_data[6])
                agc_gain = int(csi_data[7])
            else:
                fft_gain = 0
                agc_gain = 0

            fft_gains.append(fft_gain)
            agc_gains.append(agc_gain)
            csv_writer.writerow(csi_data)

            csi_data_complex[:-1] = csi_data_complex[1:]
            agc_gain_data[:-1] = agc_gain_data[1:]
            fft_gain_data[:-1] = fft_gain_data[1:]
            agc_gain_data[-1] = agc_gain
            fft_gain_data[-1] = fft_gain

            if count == 0:
                count = 1
                print('CSI I/Q length:', csi_data_len, '(ESP32-C5 LLTF is 106 — this is success)')
                if csi_data_len == 106:
                    colors = generate_subcarrier_colors((0,25), (27,53), None, len(csi_raw_data))
                elif  csi_data_len == 114:
                    colors = generate_subcarrier_colors((0,27), (29,56), None, len(csi_raw_data))
                elif  csi_data_len == 52:
                    colors = generate_subcarrier_colors((0,12), (13,26), None, len(csi_raw_data))
                elif  csi_data_len == 234 :
                    colors = generate_subcarrier_colors((0,28), (29,56), (60,116), len(csi_raw_data))
                elif  csi_data_len == 228 :
                    colors = generate_subcarrier_colors((0,28), (29,57), (57,113), len(csi_raw_data))
                elif  csi_data_len == 490 :
                    colors = generate_subcarrier_colors((0,61), (62,122), (123,245), len(csi_raw_data))
                elif  csi_data_len == 128 :
                    colors = generate_subcarrier_colors((0,31), (32,63), None, len(csi_raw_data))
                elif  csi_data_len == 256 :
                    colors = generate_subcarrier_colors((0,32), (32,63), (64,128), len(csi_raw_data))
                elif  csi_data_len == 512 :
                    colors = generate_subcarrier_colors((0,63), (64,127), (128,256), len(csi_raw_data))
                elif  csi_data_len == 384 :
                    colors = generate_subcarrier_colors((0,63), (64,127), (128,192), len(csi_raw_data))
                elif csi_data_len > 0 and csi_data_len <= 612:
                    raw_len = len(csi_raw_data)
                    colors = generate_subcarrier_colors((0,raw_len//2), (raw_len//2+1,raw_len-1), None, raw_len)
                else:
                    colors = generate_subcarrier_colors(None, None, None, len(csi_raw_data))
                if callback:
                    callback(colors)

            n = min(csi_data_len // 2, CSI_DATA_COLUMNS)
            for i in range(n):
                csi_data_complex[-1][i] = complex(csi_raw_data[i * 2 + 1],
                                                csi_raw_data[i * 2])
    ser.close()
    return


class SubThread (QThread):
    data_ready = pyqtSignal(object)
    def __init__(self, serial_port, save_file_name, log_file_name):
        super().__init__()
        self.serial_port = serial_port

        save_file_fd = open(save_file_name, 'w')
        self.log_file_fd = open(log_file_name, 'w')
        self.csv_writer = csv.writer(save_file_fd)
        self.csv_writer.writerow(DATA_COLUMNS_NAMES_C5C6)

    def run(self):
        csi_data_read_parse(self.serial_port, self.csv_writer, self.log_file_fd,callback=self.data_ready.emit)

    def __del__(self):
        self.wait()
        self.log_file_fd.close()


if __name__ == '__main__':
    if sys.version_info < (3, 6):
        print(' Python version should >= 3.6')
        exit()

    parser = argparse.ArgumentParser(
        description='Read CSI data from serial port and display it graphically')
    parser.add_argument('-p', '--port', dest='port', action='store', required=True,
                        help='Serial port number of csv_recv device')
    parser.add_argument('-s', '--store', dest='store_file', action='store', default='./csi_data.csv',
                        help='Save the data printed by the serial port to a file')
    parser.add_argument('-l', '--log', dest='log_file', action='store', default='./csi_data_log.txt',
                        help='Save other serial data the bad CSI data to a log file')

    args = parser.parse_args()
    serial_port = args.port
    file_name = args.store_file
    log_file_name = args.log_file

    app = QApplication(sys.argv)

    subthread = SubThread(serial_port, file_name, log_file_name)

    window = csi_data_graphical_window()
    subthread.data_ready.connect(window.update_curve_colors)
    subthread.start()
    window.show()

    sys.exit(app.exec())
