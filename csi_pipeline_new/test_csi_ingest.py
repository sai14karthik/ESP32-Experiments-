#!/usr/bin/env python3
"""Ingest path checks: lab fixture parse + DB row packing (no live ESP required)."""

from __future__ import annotations

import unittest
from pathlib import Path

from csi_parse import iq_to_amplitudes, parse_csi_line

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "sample_csi_lines.csv"


def _row_tuple(sample: dict) -> tuple:
    """Mirror ingest_serial.flush_batch column order."""
    return (
        sample["seq"],
        sample["mac"],
        sample["rssi"],
        sample["rate"],
        sample["noise_floor"],
        sample["fft_gain"],
        sample["agc_gain"],
        sample["channel"],
        sample["device_ts"],
        sample["sig_len"],
        sample["rx_format"],
        sample["len"],
        sample["first_word"],
        sample["iq"],
    )


class TestIngestLabFixture(unittest.TestCase):
    def test_fixture_is_lab_router_and_even_iq(self) -> None:
        lines = FIXTURE.read_text(encoding="utf-8").strip().splitlines()
        self.assertGreaterEqual(len(lines), 1)
        for line in lines:
            s = parse_csi_line(line)
            self.assertIsNotNone(s)
            assert s is not None
            self.assertEqual(s["format"], "lab_router")
            iq = s["iq"]
            self.assertGreaterEqual(len(iq), 2)
            self.assertEqual(len(iq) % 2, 0)
            self.assertEqual(s["len"], len(iq))
            amps = iq_to_amplitudes(iq)
            self.assertEqual(len(amps), len(iq) // 2)
            row = _row_tuple(s)
            self.assertEqual(len(row), 14)
            self.assertIsInstance(row[-1], list)
            self.assertTrue(all(isinstance(x, int) for x in row[-1][:4]))

    def test_c5_tcp_line_field_map(self) -> None:
        # Matches csi_recv_router C5 printf (FORCE_LLTF=0): len = info->len bytes.
        line = (
            'CSI_DATA,3,94:d9:b3:80:8c:81,-31,11,-93,0,0,13,2810526,67,0,8,1,'
            '"[67,48,4,0,0,0,5,0]"'
        )
        s = parse_csi_line(line)
        self.assertIsNotNone(s)
        assert s is not None
        self.assertEqual(s["format"], "lab_router")
        self.assertEqual(s["seq"], 3)
        self.assertEqual(s["mac"], "94:d9:b3:80:8c:81")
        self.assertEqual(s["rssi"], -31)
        self.assertEqual(s["noise_floor"], -93)
        self.assertEqual(s["fft_gain"], 0)
        self.assertEqual(s["agc_gain"], 0)
        self.assertEqual(s["channel"], 13)
        self.assertEqual(s["device_ts"], 2810526)
        self.assertEqual(s["sig_len"], 67)
        self.assertEqual(s["rx_format"], 0)  # cur_bb_format in firmware header
        self.assertEqual(s["len"], 8)
        self.assertEqual(s["first_word"], 1)
        self.assertEqual(len(s["iq"]), 8)
        self.assertEqual(len(iq_to_amplitudes(s["iq"])), 4)


if __name__ == "__main__":
    unittest.main()
