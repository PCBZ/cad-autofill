"""ShipmentFlow and `main.py extract`, with a fake extractor: no model, no network."""

import contextlib
import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import pymupdf

from src.schema import Shipment
from test_pdf_text import INVOICE_LINES, add_text_page, make_scan
from test_validator import make_shipment

HAVE_CREWAI = importlib.util.find_spec("crewai") is not None
SETTINGS = {
    "text": {"min_chars_per_page": 50},
    "validation": {"amount_tolerance": 0.01},
}
EMAIL = {
    "message_id": "18c2f0a9d3e4b5c6",
    "thread_id": "18c2f0a9d3e4b5c6",
    "from": "docs@client.example",
    "subject": "",
    "received": "2026-11-23T14:05:00Z",
}


class FakeExtractor:
    """Stands in for the extraction crew and remembers how it was called."""

    def __init__(self, shipment: dict):
        self.shipment = shipment
        self.calls: list[list] = []

    def __call__(self, documents):
        self.calls.append(documents)
        return Shipment.model_validate(copy.deepcopy(self.shipment))


def bad_total_shipment() -> dict:
    return make_shipment(invoice_total={"value": 9999.0, "source": "ci"})


@unittest.skipUnless(HAVE_CREWAI, "needs requirements-extract.txt")
class FlowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.shipment_dir = root / "inbox" / "20261123-abc"
        self.shipment_dir.mkdir(parents=True)
        self.output_dir = root / "output" / "20261123-abc"

    def add_pdfs(self, count: int = 3) -> None:
        for name in ("bl", "ci", "emf")[:count]:
            doc = pymupdf.open()
            add_text_page(doc, [f"{name.upper()} " + line for line in INVOICE_LINES])
            doc.save(self.shipment_dir / f"{name}.pdf")
            doc.close()

    def run_flow(self, extractor):
        from src.flow import run_extract

        return run_extract(SETTINGS, self.shipment_dir, self.output_dir, extractor=extractor)

    def test_passing_shipment_is_written(self):
        from src.flow import OUTCOME_DONE

        self.add_pdfs()
        (self.shipment_dir / "email.json").write_text(json.dumps(EMAIL))
        extractor = FakeExtractor(make_shipment())
        state = self.run_flow(extractor)
        self.assertEqual(state.outcome, OUTCOME_DONE)
        self.assertEqual(len(extractor.calls), 1)
        self.assertEqual(len(extractor.calls[0]), 3)  # all three documents were read
        saved = Shipment.model_validate_json((self.output_dir / "shipment.json").read_text())
        self.assertTrue(saved.validation.passed)
        self.assertEqual(saved.email.message_id, "18c2f0a9d3e4b5c6")  # from email.json
        self.assertIn("Result: passed", (self.output_dir / "validation_report.md").read_text())

    def test_failed_validation_stops_for_a_person(self):
        from src.flow import OUTCOME_NEEDS_REVIEW

        self.add_pdfs()
        state = self.run_flow(FakeExtractor(bad_total_shipment()))
        self.assertEqual(state.outcome, OUTCOME_NEEDS_REVIEW)
        report = (self.output_dir / "validation_report.md").read_text()
        self.assertIn("FAILED", report)
        self.assertIn("invoice_total", report)
        saved = Shipment.model_validate_json((self.output_dir / "shipment.json").read_text())
        self.assertFalse(saved.validation.passed)

    def test_scanned_pdf_stops_before_extraction(self):
        from src.flow import OUTCOME_SCANNED

        self.add_pdfs(2)
        source = pymupdf.open()
        add_text_page(source, INVOICE_LINES)
        scan = make_scan(source)
        scan.save(self.shipment_dir / "emf.pdf")
        extractor = FakeExtractor(make_shipment())
        state = self.run_flow(extractor)
        self.assertEqual(state.outcome, OUTCOME_SCANNED)
        self.assertIn("emf.pdf", state.messages[0])
        self.assertEqual(extractor.calls, [])
        self.assertFalse((self.output_dir / "shipment.json").exists())

    def test_folder_without_pdfs(self):
        from src.flow import OUTCOME_NO_PDFS

        extractor = FakeExtractor(make_shipment())
        self.assertEqual(self.run_flow(extractor).outcome, OUTCOME_NO_PDFS)
        self.assertEqual(extractor.calls, [])

    def test_unreadable_pdf(self):
        from src.flow import OUTCOME_UNREADABLE

        self.add_pdfs(2)
        (self.shipment_dir / "emf.pdf").write_text("not a pdf")
        extractor = FakeExtractor(make_shipment())
        state = self.run_flow(extractor)
        self.assertEqual(state.outcome, OUTCOME_UNREADABLE)
        self.assertIn("emf.pdf", state.messages[0])
        self.assertEqual(extractor.calls, [])

    def test_invalid_email_json_stops(self):
        from src.flow import OUTCOME_UNREADABLE

        self.add_pdfs()
        (self.shipment_dir / "email.json").write_text('{"message_id": 1}')
        extractor = FakeExtractor(make_shipment())
        self.assertEqual(self.run_flow(extractor).outcome, OUTCOME_UNREADABLE)
        self.assertEqual(extractor.calls, [])

    def test_missing_email_json_is_fine(self):
        from src.flow import OUTCOME_DONE

        self.add_pdfs()
        state = self.run_flow(FakeExtractor(make_shipment()))
        self.assertEqual(state.outcome, OUTCOME_DONE)
        self.assertIsNone(state.shipment.email)

    def test_default_extractor_says_it_is_not_implemented(self):
        from src.flow import run_extract

        self.add_pdfs()
        with self.assertRaisesRegex(NotImplementedError, "#9"):
            run_extract(SETTINGS, self.shipment_dir, self.output_dir)

    def test_corrections_file_resumes_without_the_extractor(self):
        self.add_pdfs()
        self.output_dir.mkdir(parents=True)
        (self.output_dir / "shipment.json").write_text(
            Shipment.model_validate(make_shipment()).model_dump_json(by_alias=True)
        )
        (self.output_dir / "corrections.toml").write_text("")
        extractor = FakeExtractor(make_shipment())
        with self.assertRaisesRegex(NotImplementedError, "#36"):
            self.run_flow(extractor)
        self.assertEqual(extractor.calls, [])

    def test_resume_without_saved_shipment_stops(self):
        from src.flow import OUTCOME_UNREADABLE

        self.output_dir.mkdir(parents=True)
        (self.output_dir / "corrections.toml").write_text("")
        state = self.run_flow(FakeExtractor(make_shipment()))
        self.assertEqual(state.outcome, OUTCOME_UNREADABLE)

    def spans(self) -> list[dict]:
        text = (self.output_dir / "trace.jsonl").read_text()
        return [json.loads(line) for line in text.splitlines()]

    def test_trace_has_steps_but_no_document_text(self):
        self.add_pdfs()
        self.run_flow(FakeExtractor(make_shipment()))
        text = (self.output_dir / "trace.jsonl").read_text()
        spans = self.spans()
        self.assertEqual(
            [s["name"] for s in spans],
            ["load", "extract", "validate", "classify", "write_excel", "run"],
        )
        load = spans[0]
        self.assertEqual(load["attributes"]["pdfs"], 3)
        self.assertEqual(load["resource"]["attributes"]["shipment"], "20261123-abc")
        self.assertEqual(spans[-1]["attributes"]["outcome"], "done")
        self.assertNotIn("INV-2026", text)
        self.assertNotIn("Shenzhen", text)

    def test_trace_marks_a_failed_step_without_the_message(self):
        from src.flow import run_extract

        self.add_pdfs()
        with self.assertRaises(NotImplementedError):
            run_extract(SETTINGS, self.shipment_dir, self.output_dir)
        spans = {s["name"]: s for s in self.spans()}
        self.assertEqual(spans["load"]["status"]["status_code"], "UNSET")
        self.assertEqual(spans["extract"]["status"]["status_code"], "ERROR")
        self.assertEqual(spans["extract"]["status"]["description"], "NotImplementedError")
        self.assertEqual(spans["extract"]["events"], [])  # no exception text recorded
        self.assertEqual(spans["run"]["status"]["status_code"], "ERROR")


@unittest.skipUnless(HAVE_CREWAI, "needs requirements-extract.txt")
class MainExtractTests(unittest.TestCase):
    """`main.extract` end to end: exit codes and what it tells the person."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.shipment_dir = self.root / "inbox" / "s1"
        self.shipment_dir.mkdir(parents=True)
        self.settings = {**SETTINGS, "paths": {"output": str(self.root / "output")}}

    def run_main(self) -> tuple[int, str, str]:
        import main

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main.extract(self.settings, self.shipment_dir)
        return code, out.getvalue(), err.getvalue()

    def test_extraction_crew_not_ready_exits_3(self):
        doc = pymupdf.open()
        add_text_page(doc, INVOICE_LINES)
        doc.save(self.shipment_dir / "ci.pdf")
        code, _, err = self.run_main()
        self.assertEqual(code, 3)
        self.assertIn("#9", err)

    def test_scanned_pdf_exits_4(self):
        source = pymupdf.open()
        add_text_page(source, INVOICE_LINES)
        make_scan(source).save(self.shipment_dir / "ci.pdf")
        code, out, _ = self.run_main()
        self.assertEqual(code, 4)
        self.assertIn("ci.pdf", out)

    def test_empty_folder_exits_4(self):
        self.assertEqual(self.run_main()[0], 4)


if __name__ == "__main__":
    unittest.main()
