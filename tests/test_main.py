"""main.py forces the CrewAI switches off, whatever the environment says."""

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EXPECTED = {
    "CREWAI_DISABLE_TELEMETRY": "true",
    "OTEL_SDK_DISABLED": "true",
    "CREWAI_TRACING_ENABLED": "false",
    "CREWAI_DISABLE_VERSION_CHECK": "true",
}


class CrewAiSwitchTests(unittest.TestCase):
    def test_switches_are_the_expected_ones(self):
        import main

        self.assertEqual(main.CREWAI_OFF, EXPECTED)

    def test_import_overrides_what_the_environment_says(self):
        import main

        wrong = {k: "false" if v == "true" else "true" for k, v in EXPECTED.items()}
        with patch.dict(os.environ, wrong):
            importlib.reload(main)
            for name, value in EXPECTED.items():
                self.assertEqual(os.environ[name], value, name)

    def test_env_example_lists_every_switch(self):
        text = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
        for name, value in EXPECTED.items():
            self.assertIn(f"{name}={value}", text)


if __name__ == "__main__":
    unittest.main()
