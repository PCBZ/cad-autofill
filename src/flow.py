"""ShipmentFlow: the fixed order of the extract command.

    load -> extract -> validate -+- passed -> classify -> write_csv -> done
                                 `- failed -> write_review -> stop for a person
    load (corrections.toml found) -> apply_corrections -> validate -> ...

Only extraction needs a model, so it is the one step passed in from outside
(`extractor`): the extraction crew (src/crews/extraction) in main.py, a fake in tests. The other
steps are code. Steps that belong to later issues are stubs that say so:
apply_corrections and the full review files (#36), classify (#18, #19), write_csv (#11).

The Flow state is not persisted: a stopped run resumes from output/<shipment>/shipment.json.
"""

from collections.abc import Callable
from pathlib import Path

from crewai.flow.flow import Flow, listen, or_, router, start
from opentelemetry.trace import Span

from src import validator
from src.schema import Email, Shipment, ShipmentState
from src.telemetry import RunTrace
from src.text.pdf_text import PdfReadError, PdfText, read_pdf

OUTCOME_DONE = "done"
OUTCOME_NO_PDFS = "no_pdfs"
OUTCOME_UNREADABLE = "unreadable"
OUTCOME_SCANNED = "scanned"
OUTCOME_NEEDS_REVIEW = "needs_review"
OUTCOME_EXTRACTION_FAILED = "extraction_failed"

Extractor = Callable[[list[PdfText]], Shipment]


class ExtractionError(Exception):
    """An extractor could not produce a shipment from these documents (which PDF is
    which is unclear, or the model's answers kept failing the format checks). The run
    stops for a person; other errors, such as a network failure, are not caught."""


class ShipmentFlow(Flow[ShipmentState]):
    def __init__(
        self,
        *,
        shipment_dir: Path,
        output_dir: Path,
        settings: dict,
        extractor: Extractor,
    ):
        super().__init__(suppress_flow_events=True)
        self.shipment_dir = shipment_dir
        self.output_dir = output_dir
        self.settings = settings
        self.extractor = extractor
        self.trace = RunTrace(output_dir / "trace.jsonl", shipment=shipment_dir.name)
        self.documents: list[PdfText] = []
        self.email: Email | None = None
        self.state.shipment_dir = str(shipment_dir)

    # --- load -------------------------------------------------------------------

    @start()
    def load(self) -> None:
        with self.trace.step("load") as span:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            if (self.output_dir / "corrections.toml").exists():
                span.set_attribute("mode", "resume")
                saved = self.output_dir / "shipment.json"
                if not saved.exists():
                    return self._stop(
                        OUTCOME_UNREADABLE, f"corrections.toml found but {saved} is missing"
                    )
                self.state.shipment = Shipment.model_validate_json(
                    saved.read_text(encoding="utf-8")
                )
                return
            span.set_attribute("mode", "fresh")
            self._read_inputs(span)

    def _read_inputs(self, span: Span) -> None:
        pdfs = sorted(p for p in self.shipment_dir.iterdir() if p.suffix.lower() == ".pdf")
        span.set_attribute("pdfs", len(pdfs))
        if not pdfs:
            return self._stop(OUTCOME_NO_PDFS, f"no PDF files in {self.shipment_dir}")
        min_chars = self.settings["text"]["min_chars_per_page"]
        documents = []
        for pdf in pdfs:
            try:
                documents.append(read_pdf(pdf, min_chars_per_page=min_chars))
            except PdfReadError as e:
                return self._stop(OUTCOME_UNREADABLE, str(e))
        scanned = [Path(d.path).name for d in documents if d.scanned]
        if scanned:
            return self._stop(
                OUTCOME_SCANNED,
                "scanned PDF, no text layer to read; a person needs to handle it: "
                + ", ".join(scanned),
            )
        self.documents = documents
        email_file = self.shipment_dir / "email.json"
        if email_file.exists():
            try:
                self.email = Email.model_validate_json(email_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                return self._stop(OUTCOME_UNREADABLE, f"email.json is not valid: {e}")

    def _stop(self, outcome: str, message: str) -> None:
        self.state.outcome = outcome
        self.state.messages.append(message)

    @router(load)
    def route(self) -> str:
        if self.state.outcome:
            return "stopped"
        return "resume" if self.state.shipment else "fresh"

    # --- extract ----------------------------------------------------------------

    @listen("fresh")
    def extract(self) -> None:
        with self.trace.step("extract") as span:
            try:
                shipment = self.extractor(self.documents)
            except ExtractionError as e:
                span.set_attribute("failed", type(e).__name__)
                return self._stop(OUTCOME_EXTRACTION_FAILED, str(e))
            if shipment.email is None:
                shipment.email = self.email
            self.state.shipment = shipment
            span.set_attribute("lines", len(shipment.lines))

    @listen("resume")
    def apply_corrections(self) -> None:
        raise NotImplementedError(
            "applying corrections.toml is not implemented yet (see issue #36)"
        )

    # --- validate ---------------------------------------------------------------

    @router(or_(extract, apply_corrections))
    def validate(self) -> str:
        if self.state.outcome:  # extract stopped
            return "stopped"
        with self.trace.step("validate") as span:
            tolerance = self.settings["validation"]["amount_tolerance"]
            result = validator.validate(self.state.shipment, amount_tolerance=tolerance)
            self.state.shipment.validation = result
            span.set_attribute("open_issues", len(result.open_issues))
            return "passed" if result.passed else "failed"

    @listen("failed")
    def write_review(self) -> None:
        # Stub: saves the shipment and a short report. The full review files
        # (corrections.toml with one entry per open issue) are issue #36.
        with self.trace.step("write_review"):
            self._save()
            self._stop(
                OUTCOME_NEEDS_REVIEW,
                f"validation failed, {len(self.state.shipment.validation.open_issues)} open "
                f"issue(s); see {self.output_dir / 'validation_report.md'} "
                "(corrections.toml is not written yet, issue #36)",
            )

    # --- classify, write_csv --------------------------------------------------

    @listen("passed")
    def classify(self) -> None:
        with self.trace.step("classify") as span:
            span.set_attribute("skipped", "Jev classification is milestone M2 (issues #18, #19)")

    @listen(classify)
    def write_csv(self) -> None:
        with self.trace.step("write_csv") as span:
            span.set_attribute("skipped", "csv_writer is issue #11")
            self._save()
            self.state.outcome = OUTCOME_DONE
            self.state.messages.append(
                f"done: {self.output_dir / 'shipment.json'} (no csv yet, issue #11)"
            )

    # --- outputs ----------------------------------------------------------------

    def _save(self) -> None:
        shipment = self.state.shipment
        (self.output_dir / "shipment.json").write_text(
            shipment.model_dump_json(indent=2, by_alias=True, exclude_none=True), encoding="utf-8"
        )
        (self.output_dir / "validation_report.md").write_text(
            _report(self.shipment_dir.name, shipment), encoding="utf-8"
        )


def _report(name: str, shipment: Shipment) -> str:
    """A short validation report. The detailed one (value per document and page) is issue #36."""
    validation = shipment.validation
    lines = [f"# Validation report: {name}", ""]
    lines.append("Result: " + ("passed" if validation.passed else "FAILED"))
    for title, issues in (
        ("Open issues", validation.open_issues),
        ("Accepted issues", [i for i in validation.issues if i.accepted]),
    ):
        if issues:
            lines += ["", f"## {title}", ""]
            lines += [f"- `{i.field}`: {i.issue}. Found: {i.found}" for i in issues]
    return "\n".join(lines) + "\n"


def run_extract(
    settings: dict, shipment_dir: Path, output_dir: Path, *, extractor: Extractor
) -> ShipmentState:
    """Run the extract command for one shipment folder and return the final state."""
    flow = ShipmentFlow(
        shipment_dir=shipment_dir, output_dir=output_dir, settings=settings, extractor=extractor
    )
    try:
        with flow.trace.step("run") as span:
            flow.kickoff()
            span.set_attribute("outcome", flow.state.outcome)
    finally:
        flow.trace.close()
    return flow.state
