"""The extraction crew with a fake model: no network, no tokens.

FakeLLM answers each task from a queue of prepared JSON replies, so the tests run the
real CrewAI crew (parallel specialists, guardrail retries, reconciler context) without
calling a model.
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

HAVE_CREWAI = importlib.util.find_spec("crewai") is not None
SETTINGS = {
    "models": {"extraction": "openrouter/anthropic/claude-haiku-5.5"},
    "crews": {"guardrail_max_retries": 1},
    "validation": {"amount_tolerance": 0.01},
}


def pdf(name: str, *pages: str, tables: dict[int, list[list[str]]] | None = None):
    from src.text.pdf_text import PdfPage, PdfTable, PdfText

    tables = tables or {}
    return PdfText(
        path=f"/inbox/s1/{name}",
        scanned=False,
        pages=[
            PdfPage(
                number=n,
                text=text,
                chars=len(text),
                tables=[PdfTable(rows=tables[n])] if n in tables else [],
            )
            for n, text in enumerate(pages, start=1)
        ],
    )


def documents():
    return [
        pdf("a.pdf", "BILL OF LADING\nB/L No: MAEU123456789\nConsignee: Acme Co., Ltd."),
        pdf(
            "b.pdf",
            "COMMERCIAL INVOICE\nInvoice No: INV-1\nTerms {FOB} as agreed",
            "page two",
            tables={2: [["No", "Description", "Amount"], ["1", "bolts", "250.00"]]},
        ),
        pdf("c.pdf", "eManifest\nCargo Control Number 1234PARS0001"),
    ]


def r(value, page=1) -> dict:
    return {"value": value, "page": page}


BL = {
    "bl_number": r("MAEU123456789"),
    "shipper": r("Shenzhen Parts Co."),
    "consignee": r("Acme Co., Ltd."),
    "packages": r(20),
    "package_type": r("CTN"),
    "gross_weight": r(1240),
}
LINES = [
    {
        "line_no": 1,
        "description": "bolts",
        "hs_code": "7318.15",
        "country_of_origin": "CN",
        "quantity": 100,
        "unit": "PCS",
        "unit_price": 2.5,
        "amount": 250.0,
    },
    {
        "line_no": 2,
        "description": "nuts",
        "hs_code": "7318.16",
        "country_of_origin": "CN",
        "quantity": 900,
        "unit": "PCS",
        "unit_price": 2.5,
        "amount": 2250.0,
    },
]
CI = {
    "invoice_number": r("INV-1"),
    "invoice_date": r("2026-11-20"),
    "currency": r("USD"),
    "invoice_total": r(2500.0, 2),
    "consignee": r("ACME CO LTD"),
}
CI_LINES = {"lines": LINES}
EMF = {
    "cargo_control_number": r("1234PARS0001"),
    "eta": r("2026-11-30T08:00"),
    "bl_number": r("maeu123456789"),
    "packages": r(20),
    "gross_weight": r(1240.0),
}
RECONCILED = {
    "bl_number": {"value": "MAEU123456789", "source": "bl"},
    "consignee": {"value": "Acme Co., Ltd.", "source": "bl"},
    "packages": {"value": 20, "source": "bl"},
    "gross_weight": {"value": 1240, "source": "bl"},
    "notes": [],
}

if HAVE_CREWAI:
    from crewai.llms.base_llm import BaseLLM

    class FakeLLM(BaseLLM):
        """Replies per task name from `replies`; the last reply repeats."""

        replies: dict[str, list[str]] = {}
        prompts: dict[str, list[str]] = {}

        def call(
            self,
            messages,
            tools=None,
            callbacks=None,
            available_functions=None,
            from_task=None,
            from_agent=None,
            response_model=None,
        ):
            name = from_task.name
            self.prompts.setdefault(name, []).append(json.dumps(messages))
            queue = self.replies[name]
            return queue.pop(0) if len(queue) > 1 else queue[0]


def fake_llm(**overrides) -> "FakeLLM":
    replies = {"bl": BL, "ci": CI, "ci_lines": CI_LINES, "emf": EMF, "reconciler": RECONCILED}
    queues = {}
    for name, reply in replies.items():
        given = overrides.get(name, [reply])
        queues[name] = [x if isinstance(x, str) else json.dumps(x) for x in given]
    return FakeLLM(model="fake", replies=queues, prompts={})


@unittest.skipUnless(HAVE_CREWAI, "needs requirements-extract.txt")
class IdentifyTests(unittest.TestCase):
    def phrases(self):
        from src.crews.extraction.crew import document_phrases

        return document_phrases()

    def test_each_pdf_gets_its_type(self):
        from src.crews.extraction.crew import identify

        by_kind = identify(documents(), self.phrases())
        self.assertEqual(
            {k: Path(d.path).name for k, d in by_kind.items()},
            {"bl": "a.pdf", "ci": "b.pdf", "emf": "c.pdf"},
        )

    def test_two_invoices_stop(self):
        from src.crews.extraction.crew import identify
        from src.flow import ExtractionError

        docs = documents()
        docs[2] = pdf("c.pdf", "COMMERCIAL INVOICE\nInvoice No: INV-2")
        with self.assertRaisesRegex(ExtractionError, r"commercial invoice: b\.pdf, c\.pdf"):
            identify(docs, self.phrases())

    def test_unknown_document_stops(self):
        from src.crews.extraction.crew import identify
        from src.flow import ExtractionError

        docs = documents()
        docs[0] = pdf("a.pdf", "Packing list")
        with self.assertRaisesRegex(ExtractionError, r"cannot tell what a\.pdf is"):
            identify(docs, self.phrases())

    def test_wrong_number_of_pdfs_stops(self):
        from src.crews.extraction.crew import identify
        from src.flow import ExtractionError

        with self.assertRaisesRegex(ExtractionError, "expected 3 PDFs"):
            identify(documents()[:2], self.phrases())


@unittest.skipUnless(HAVE_CREWAI, "needs requirements-extract.txt")
class RenderTests(unittest.TestCase):
    def test_pages_and_tables_are_marked(self):
        from src.crews.extraction.crew import render

        text = render(documents()[1])
        self.assertIn("=== page 1 ===\nCOMMERCIAL INVOICE", text)
        self.assertIn("=== page 2 ===\npage two", text)
        self.assertIn(
            "--- table 1 on page 2 ---\nNo | Description | Amount\n1 | bolts | 250.00", text
        )
        self.assertIn("{FOB}", text)


@unittest.skipUnless(HAVE_CREWAI, "needs requirements-extract.txt")
class GuardrailTests(unittest.TestCase):
    def run_guardrail(self, model, check, data):
        from src.crews.extraction.guardrails import make_guardrail

        raw = data if isinstance(data, str) else json.dumps(data)
        return make_guardrail(model, check)(mock.Mock(raw=raw))

    def ci_problems(self, **changes):
        from src.crews.extraction.guardrails import check_ci
        from src.crews.extraction.outputs import CIReading

        return check_ci(CIReading.model_validate({**CI, **changes}), 2)

    def test_good_answer_passes_as_the_model(self):
        from src.crews.extraction.guardrails import check_ci
        from src.crews.extraction.outputs import CIReading

        ok, result = self.run_guardrail(CIReading, lambda m: check_ci(m, 2), CI)
        self.assertTrue(ok)
        self.assertIsInstance(result, CIReading)

    def test_json_in_a_code_fence_passes(self):
        from src.crews.extraction.outputs import EMFReading

        ok, _ = self.run_guardrail(
            EMFReading, lambda m: [], "```json\n" + json.dumps(EMF) + "\n```"
        )
        self.assertTrue(ok)

    def test_not_json_or_unknown_field_fails(self):
        from src.crews.extraction.outputs import BLReading

        for raw in ("Here are the fields: ...", json.dumps({**BL, "notify": r("x")})):
            ok, reason = self.run_guardrail(BLReading, lambda m: [], raw)
            self.assertFalse(ok)
            self.assertIn("JSON format", reason)

    def test_check_failures_are_listed_for_the_retry(self):
        from src.crews.extraction.guardrails import check_bl
        from src.crews.extraction.outputs import BLReading

        ok, reason = self.run_guardrail(
            BLReading,
            lambda m: check_bl(m, 1),
            {**BL, "bl_number": r(""), "packages": r("20 CTNS")},
        )
        self.assertFalse(ok)
        self.assertIn("bl_number is missing", reason)
        self.assertIn("packages: '20 CTNS' must be a JSON number", reason)

    def test_ci_formats(self):
        self.assertEqual(self.ci_problems(), [])
        self.assertIn("three-letter", self.ci_problems(currency=r("US$"))[0])
        self.assertIn("YYYY-MM-DD", self.ci_problems(invoice_date=r("20/11/2026"))[0])
        self.assertIn("YYYY-MM-DD", self.ci_problems(invoice_date=r("20261120"))[0])

    def test_ci_lines_formats(self):
        from src.crews.extraction.guardrails import check_ci_lines
        from src.crews.extraction.outputs import CILines

        def problems(lines):
            return check_ci_lines(CILines(lines=lines), 2)

        self.assertEqual(problems(LINES), [])
        self.assertIn("no invoice lines", problems([])[0])
        self.assertIn("1, 2, 3", problems([LINES[1]])[0])
        china = [{**LINES[0], "country_of_origin": "China"}, LINES[1]]
        self.assertIn("two-letter", problems(china)[0])

    def test_page_past_the_end_fails(self):
        self.assertIn("page 5", self.ci_problems(invoice_number=r("INV-1", 5))[0])

    def test_eta_may_have_a_time(self):
        from src.crews.extraction.guardrails import check_date
        from src.crews.extraction.outputs import Reading

        self.assertEqual(
            check_date("eta", Reading(value="2026-11-30T08:00"), time_allowed=True), []
        )
        self.assertEqual(check_date("eta", Reading(value=""), time_allowed=True), [])
        self.assertTrue(check_date("invoice_date", Reading(value="2026-11-30T08:00")))

    def test_reconciler_must_pick_a_document_that_gave_the_field(self):
        from src.crews.extraction.guardrails import check_reconciled
        from src.crews.extraction.outputs import BLReading, CIReading, EMFReading, Reconciled

        readings = {
            "bl": BLReading.model_validate(BL),
            "ci": CIReading.model_validate(CI),
            "emf": EMFReading.model_validate({**EMF, "packages": r("")}),
        }
        good = Reconciled.model_validate(RECONCILED)
        self.assertEqual(check_reconciled(good, readings), [])
        bad = Reconciled.model_validate(
            {
                **RECONCILED,
                "packages": {"value": 20, "source": "emf"},
                "consignee": {"value": "Acme", "source": "emf"},
            }
        )
        problems = check_reconciled(bad, readings)
        self.assertEqual(len(problems), 2)
        self.assertIn("consignee: emf does not give this field", problems)


@unittest.skipUnless(HAVE_CREWAI, "needs requirements-extract.txt")
class BuildShipmentTests(unittest.TestCase):
    def build(self, rec=RECONCILED, **changes):
        from src.crews.extraction.crew import build_shipment
        from src.crews.extraction.outputs import (
            BLReading,
            CILines,
            CIReading,
            EMFReading,
            Reconciled,
        )

        return build_shipment(
            BLReading.model_validate(changes.get("bl", BL)),
            CIReading.model_validate(changes.get("ci", CI)),
            CILines.model_validate(changes.get("ci_lines", CI_LINES)),
            EMFReading.model_validate(changes.get("emf", EMF)),
            Reconciled.model_validate(rec),
        )

    def test_found_holds_each_documents_reading(self):
        header = self.build().header
        self.assertEqual(header.bl_number.found, {"bl": "MAEU123456789", "emf": "maeu123456789"})
        self.assertEqual(header.consignee.found, {"bl": "Acme Co., Ltd.", "ci": "ACME CO LTD"})
        self.assertEqual(header.packages.value, 20)
        self.assertEqual(header.packages.source, "bl")

    def test_every_header_field_has_a_source(self):
        header = self.build().header
        for name, field in header:
            self.assertIsNotNone(field, name)
            self.assertIn(field.source, ("bl", "ci", "emf"), name)
        self.assertEqual(header.cargo_control_number.source, "emf")
        self.assertEqual(header.currency.source, "ci")
        self.assertEqual(header.vessel_voyage.value, "")  # not on the B/L

    def test_reconciler_pick_is_used(self):
        rec = {**RECONCILED, "gross_weight": {"value": 1240.0, "source": "emf"}}
        weight = self.build(rec).header.gross_weight
        self.assertEqual((weight.value, weight.source), (1240.0, "emf"))

    def test_without_a_pick_the_bl_reading_is_used(self):
        rec = {"notes": []}
        header = self.build(rec, bl={**BL, "packages": r("")}).header
        self.assertEqual(header.bl_number.source, "bl")
        self.assertEqual((header.packages.value, header.packages.source), (20, "emf"))

    def test_pick_from_a_document_without_a_reading_is_ignored(self):
        rec = {**RECONCILED, "packages": {"value": 99, "source": "emf"}}
        packages = self.build(rec, emf={**EMF, "packages": r("")}).header.packages
        self.assertEqual((packages.value, packages.source), (20, "bl"))

    def test_lines_and_total_come_from_the_invoice(self):
        shipment = self.build()
        self.assertEqual(len(shipment.lines), 2)
        self.assertEqual((shipment.invoice_total.value, shipment.invoice_total.page), (2500.0, 2))

    def test_result_passes_the_validator(self):
        from src.validator import validate

        self.assertTrue(validate(self.build(), amount_tolerance=0.01).passed)


@unittest.skipUnless(HAVE_CREWAI, "needs requirements-extract.txt")
class CrewRunTests(unittest.TestCase):
    """The real crew, with FakeLLM in place of the model."""

    def extract(self, llm):
        from src.crews.extraction.crew import extract_shipment

        return extract_shipment(documents(), SETTINGS, llm=llm)

    def test_shipment_from_the_four_tasks(self):
        llm = fake_llm()
        shipment = self.extract(llm)
        self.assertEqual(shipment.header.bl_number.value, "MAEU123456789")
        self.assertEqual(shipment.header.cargo_control_number.value, "1234PARS0001")
        self.assertEqual(len(shipment.lines), 2)
        self.assertEqual(sorted(llm.prompts), ["bl", "ci", "ci_lines", "emf", "reconciler"])

    def test_each_specialist_sees_only_its_document(self):
        llm = fake_llm()
        self.extract(llm)
        self.assertIn("MAEU123456789", llm.prompts["bl"][0])
        self.assertNotIn("1234PARS0001", llm.prompts["bl"][0])
        self.assertIn("{FOB}", llm.prompts["ci"][0])  # braces in a document are kept
        self.assertIn("1234PARS0001", llm.prompts["emf"][0])

    def test_reconciler_sees_the_readings(self):
        llm = fake_llm()
        self.extract(llm)
        prompt = llm.prompts["reconciler"][0]
        self.assertIn("ACME CO LTD", prompt)
        self.assertIn("maeu123456789", prompt)

    def test_a_rejected_answer_is_retried(self):
        llm = fake_llm(ci=[{**CI, "currency": r("US Dollars")}, CI])
        shipment = self.extract(llm)
        self.assertEqual(len(llm.prompts["ci"]), 2)
        self.assertIn("three-letter", llm.prompts["ci"][1])
        self.assertEqual(shipment.header.currency.value, "USD")

    def test_rejected_lines_do_not_make_the_header_read_again(self):
        llm = fake_llm(ci_lines=[{"lines": [LINES[1]]}, CI_LINES])
        shipment = self.extract(llm)
        self.assertEqual(len(llm.prompts["ci_lines"]), 2)
        self.assertIn("1, 2, 3", llm.prompts["ci_lines"][1])
        self.assertEqual(len(llm.prompts["ci"]), 1)
        self.assertEqual([line.line_no for line in shipment.lines], [1, 2])

    def test_reconciler_does_not_get_the_lines(self):
        llm = fake_llm()
        self.extract(llm)
        self.assertNotIn("7318.15", llm.prompts["reconciler"][0])

    def test_expected_output_reaches_the_model_unchanged(self):
        llm = fake_llm()
        self.extract(llm)
        self.assertIn('{\\"value\\": \\"\\", \\"page\\": null}', llm.prompts["bl"][0])

    def test_answers_that_keep_failing_stop_for_a_person(self):
        from src.flow import ExtractionError

        llm = fake_llm(emf=[{**EMF, "cargo_control_number": r("")}])
        with self.assertRaisesRegex(ExtractionError, "format checks"):
            self.extract(llm)

    def test_task_outputs_are_not_saved_by_crewai(self):
        from crewai.memory.storage.kickoff_task_outputs_storage import (
            KickoffTaskOutputsSQLiteStorage,
        )

        with mock.patch.object(
            KickoffTaskOutputsSQLiteStorage, "__init__", side_effect=AssertionError("db opened")
        ):
            self.extract(fake_llm())


@unittest.skipUnless(HAVE_CREWAI, "needs requirements-extract.txt")
class ConfigTests(unittest.TestCase):
    def test_every_agent_and_task_has_its_text(self):
        import yaml

        config = HERE.parent / "src" / "crews" / "extraction" / "config"
        agents = yaml.safe_load((config / "agents.yaml").read_text(encoding="utf-8"))
        tasks = yaml.safe_load((config / "tasks.yaml").read_text(encoding="utf-8"))
        for name in ("bl", "ci", "ci_lines", "emf", "reconciler"):
            self.assertEqual(set(agents[name]), {"role", "goal", "backstory"}, name)
            self.assertEqual(set(tasks[name]), {"description", "expected_output"}, name)
        for name in ("bl", "ci", "ci_lines", "emf"):
            self.assertIn("{document}", tasks[name]["description"])
            self.assertIn("{rules}", tasks[name]["description"])

    def test_every_document_type_has_title_phrases(self):
        import tomllib

        path = HERE.parent / "src" / "crews" / "extraction" / "config" / "documents.toml"
        with path.open("rb") as f:
            documents = tomllib.load(f)
        self.assertEqual(set(documents), {"bl", "ci", "emf"})
        for kind, table in documents.items():
            self.assertEqual(set(table), {"phrases"}, kind)
            self.assertTrue(table["phrases"], kind)
            self.assertTrue(all(isinstance(p, str) and p for p in table["phrases"]), kind)


if __name__ == "__main__":
    unittest.main()
