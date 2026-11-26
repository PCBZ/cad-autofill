"""telemetry.py: spans go to a local file, and only what a step adds."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.telemetry import RunTrace


class RunTraceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "out" / "s1" / "trace.jsonl"

    def spans(self) -> list[dict]:
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_one_json_line_per_step_with_facts(self):
        trace = RunTrace(self.path, shipment="s1")
        with trace.step("load") as span:
            span.set_attribute("pdfs", 3)
        with trace.step("validate"):
            pass
        trace.close()
        first, second = self.spans()
        self.assertEqual((first["name"], second["name"]), ("load", "validate"))
        self.assertEqual(first["attributes"], {"pdfs": 3})
        self.assertEqual(first["resource"]["attributes"]["service.name"], "cad-autofill")
        self.assertEqual(first["status"]["status_code"], "UNSET")
        self.assertGreaterEqual(first["end_time"], first["start_time"])

    def test_creates_the_output_folder(self):
        RunTrace(self.path).close()
        self.assertTrue(self.path.parent.is_dir())

    def test_error_keeps_the_type_but_not_the_message_or_stack(self):
        trace = RunTrace(self.path)
        with self.assertRaises(ValueError):
            with trace.step("extract"):
                raise ValueError("invoice total 2,500.00 for Acme Imports is wrong")
        trace.close()
        (span,) = self.spans()
        self.assertEqual(span["status"]["status_code"], "ERROR")
        self.assertEqual(span["status"]["description"], "ValueError")
        self.assertEqual(span["events"], [])
        self.assertNotIn("Acme", self.path.read_text())

    def test_a_second_run_appends(self):
        for _ in range(2):
            trace = RunTrace(self.path)
            with trace.step("load"):
                pass
            trace.close()
        self.assertEqual(len(self.spans()), 2)

    def test_sdk_disabled_environment_would_silence_it(self):
        # Why main.py removes OTEL_SDK_DISABLED: with it set, the SDK records nothing.
        with patch.dict(os.environ, {"OTEL_SDK_DISABLED": "true"}):
            trace = RunTrace(self.path)
            with trace.step("load"):
                pass
            trace.close()
        self.assertEqual(self.path.read_text(), "")


if __name__ == "__main__":
    unittest.main()
