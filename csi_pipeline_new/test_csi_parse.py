#!/usr/bin/env python3
"""Unit tests for CSI parse + amplitude conversion (no hardware)."""

from __future__ import annotations

import math
import unittest

from csi_parse import iq_to_amplitudes, parse_csi_line


def _xiao_line(iq: list[int], *, seq: int = 42, rssi: int = -63) -> str:
    blob = " ".join(str(x) for x in iq)
    return (
        f"CSI_DATA,58:e6:c5:12:83:c8,{rssi},11,-95,1,0,2,1,100,0,"
        f"123456,{seq},0,{len(iq)},[{blob}]"
    )


def _lab_line(iq: list[int], *, seq: int = 7) -> str:
    blob = ",".join(str(x) for x in iq)
    return (
        f'CSI_DATA,{seq},2c:67:be:40:eb:a0,-55,14,-97,0,0,1,1000,67,0,'
        f'{len(iq)},0,"[{blob}]"'
    )


class TestIqAmplitudes(unittest.TestCase):
    def test_imag_real_hypot(self) -> None:
        # imag=3, real=4 → 5; imag=0, real=0 → 0
        amps = iq_to_amplitudes([3, 4, 0, 0])
        self.assertEqual(len(amps), 2)
        self.assertAlmostEqual(amps[0], 5.0)
        self.assertAlmostEqual(amps[1], 0.0)

    def test_odd_length_drops_tail(self) -> None:
        amps = iq_to_amplitudes([3, 4, 5])
        self.assertEqual(len(amps), 1)
        self.assertAlmostEqual(amps[0], 5.0)

    def test_empty(self) -> None:
        self.assertEqual(iq_to_amplitudes([]), [])
        self.assertEqual(iq_to_amplitudes([1]), [])


class TestParseFormats(unittest.TestCase):
    def test_xiao_c6_fields(self) -> None:
        iq = []
        for k in range(8):
            iq.extend([3 * k, 4 * k])
        s = parse_csi_line(_xiao_line(iq, seq=99, rssi=-70))
        assert s is not None
        self.assertEqual(s["format"], "xiao_c6")
        self.assertEqual(s["mac"], "58:e6:c5:12:83:c8")
        self.assertEqual(s["rssi"], -70)
        self.assertEqual(s["channel"], 1)
        self.assertEqual(s["seq"], 99)
        self.assertEqual(len(s["iq"]), 16)
        amps = iq_to_amplitudes(s["iq"])
        self.assertEqual(len(amps), 8)
        self.assertAlmostEqual(amps[1], 5.0)

    def test_lab_router(self) -> None:
        iq = [3, 4, 6, 8]
        s = parse_csi_line(_lab_line(iq))
        assert s is not None
        self.assertEqual(s["format"], "lab_router")
        self.assertEqual(s["seq"], 7)
        self.assertEqual(s["mac"], "2c:67:be:40:eb:a0")
        amps = iq_to_amplitudes(s["iq"])
        self.assertEqual(len(amps), 2)
        self.assertAlmostEqual(amps[0], 5.0)
        self.assertAlmostEqual(amps[1], 10.0)

    def test_hernandez_role(self) -> None:
        line = (
            "CSI_DATA,STA,aa:bb:cc:dd:ee:ff,-40,11,1,6,1,0,1,0,1,0,0,-93,0,13,2,"
            "2751923,0,67,0,4,1,[3 4 0 0]"
        )
        s = parse_csi_line(line)
        assert s is not None
        self.assertEqual(s["format"], "hernandez")
        self.assertEqual(s["mac"], "aa:bb:cc:dd:ee:ff")
        self.assertAlmostEqual(iq_to_amplitudes(s["iq"])[0], 5.0)

    def test_reject_truncated_iq(self) -> None:
        # Missing closing bracket → csv may still parse but blob invalid
        bad = "CSI_DATA,58:e6:c5:12:83:c8,-63,11,-95,1,0,2,1,100,0,1,1,0,2,[3 4"
        self.assertIsNone(parse_csi_line(bad))

    def test_reject_non_csi(self) -> None:
        self.assertIsNone(parse_csi_line("# csi/s=10 rx=1"))
        self.assertIsNone(parse_csi_line(""))

    def test_64_subcarrier_xiao(self) -> None:
        """Typical C6 LLTF-sized payload used by the live visualizer."""
        iq = [1, 0] * 64
        s = parse_csi_line(_xiao_line(iq))
        assert s is not None
        amps = iq_to_amplitudes(s["iq"])
        self.assertEqual(len(amps), 64)
        self.assertTrue(all(math.isclose(a, 1.0) for a in amps))


if __name__ == "__main__":
    unittest.main()
