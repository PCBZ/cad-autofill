"""Offline tests for scan verdicts; no Windows UI access."""

import unittest

from tools.scan_candata_controls import BackendMetrics, verdict


class VerdictTests(unittest.TestCase):
    def test_empty_backend_cannot_hide_unreadable_input(self):
        results = {
            "uia": BackendMetrics("uia", inputs=39, addressable=39,
                                  values_read=23, values_empty=15, values_unreadable=1),
            "win32": BackendMetrics("win32"),
        }
        report = "\n".join(verdict(results))
        self.assertIn("READ-BACK (uia): 23 input values read back; 1 inputs unreadable", report)

    def test_readback_uses_backend_with_most_inputs(self):
        results = {
            "uia": BackendMetrics("uia", inputs=5, addressable=5, values_read=5),
            "win32": BackendMetrics("win32", inputs=10, values_read=3, values_unreadable=7),
        }
        self.assertIn("READ-BACK (win32): 3 input values read back; 7 inputs unreadable",
                      "\n".join(verdict(results)))

    def test_no_readable_values_on_selected_backend(self):
        results = {
            "uia": BackendMetrics("uia", inputs=10, addressable=10, values_unreadable=10),
            "win32": BackendMetrics("win32", inputs=2, values_read=2),
        }
        self.assertIn("READ-BACK: no non-empty values were read", "\n".join(verdict(results)))

    def test_failed_backend_is_excluded(self):
        results = {
            "uia": BackendMetrics("uia", ok=False, inputs=20),
            "win32": BackendMetrics("win32", inputs=5, values_read=4, values_unreadable=1),
        }
        self.assertIn("READ-BACK (win32): 4", "\n".join(verdict(results)))


if __name__ == "__main__":
    unittest.main()
