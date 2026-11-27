"""The extraction crew: B/L, CI and EMF specialists in parallel, then a reconciler.

    documents -> which PDF is which (code) -> bl, ci, ci_lines, emf tasks (async)
              -> reconciler (bl, ci, emf) -> build_shipment (code) -> Shipment

Agent and task text is in config/agents.yaml and config/tasks.yaml next to this file (the
CrewAI convention). Settings are TOML: the title phrases that tell the PDFs apart are in
config/documents.toml, and the model is [models] extraction in settings.toml.
`make_extractor(settings)` gives the function ShipmentFlow calls in its extract step.

The crew sees the document text only. It has no tools, no memory and no delegation.
"""

import tomllib
from pathlib import Path
from typing import Any

import yaml
from crewai import LLM, Agent, Crew, Process, Task
from crewai.llms.base_llm import BaseLLM
from crewai.utilities import task_output_storage_handler
from opentelemetry import trace
from pydantic import BaseModel

from src.crews.extraction import guardrails
from src.crews.extraction.models import (
    SHARED_FIELDS,
    BLReading,
    CILines,
    CIReading,
    EMFReading,
    Reading,
    Reconciled,
    SpecialistDocument,
)
from src.flow import ExtractionError
from src.schema import Header, HeaderField, Shipment
from src.text.pdf_text import PdfText

CONFIG_DIR = Path(__file__).parent / "config"
DOCUMENTS: tuple[SpecialistDocument, ...] = ("bl", "ci", "emf")
DOCUMENT_NAMES = {"bl": "bill of lading", "ci": "commercial invoice", "emf": "eManifest"}
# The specialist tasks: name -> (document it reads, output model, guardrail check).
# The invoice has two tasks, header and lines, so each has one purpose and one output.
SPECIALISTS: dict[str, tuple[SpecialistDocument, type[BaseModel], guardrails.Check]] = {
    "bl": ("bl", BLReading, guardrails.check_bl),
    "ci": ("ci", CIReading, guardrails.check_ci),
    "ci_lines": ("ci", CILines, guardrails.check_ci_lines),
    "emf": ("emf", EMFReading, guardrails.check_emf),
}


class _NoTaskOutputStorage:
    """Stands in for CrewAI's KickoffTaskOutputsSQLiteStorage.

    Without this, every crew run saves each task's output, prompt and inputs to
    latest_kickoff_task_outputs.db in the user's application data folder, outside
    output/ and never deleted. That file only serves Crew.replay(), which we do not use,
    and CrewAI has no setting to turn it off.
    """

    def add(self, *args: Any, **kwargs: Any) -> None:
        pass

    def update(self, *args: Any, **kwargs: Any) -> None:
        pass

    def load(self) -> list[dict[str, Any]]:
        return []

    def delete_all(self) -> None:
        pass


task_output_storage_handler.KickoffTaskOutputsSQLiteStorage = _NoTaskOutputStorage


def load_config(name: str) -> dict:
    """agents.yaml or tasks.yaml: CrewAI agent and task text."""
    with (CONFIG_DIR / name).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def document_phrases() -> dict[str, list[str]]:
    """The title phrases per document type, from documents.toml (a setting, so TOML)."""
    with (CONFIG_DIR / "documents.toml").open("rb") as f:
        return {kind: table["phrases"] for kind, table in tomllib.load(f).items()}


# --- which PDF is which ---------------------------------------------------------------


def identify(documents: list[PdfText], phrases: dict[str, list[str]]) -> dict[str, PdfText]:
    """Map bl/ci/emf to a PDF by the title phrases on its first page (documents.toml)."""
    if len(documents) != len(DOCUMENTS):
        raise ExtractionError(
            f"expected 3 PDFs (B/L, commercial invoice, eManifest), found {len(documents)}: "
            + ", ".join(Path(d.path).name for d in documents)
        )
    found: dict[str, list[str]] = {kind: [] for kind in DOCUMENTS}
    unknown = []
    by_kind: dict[str, PdfText] = {}
    for doc in documents:
        first_page = doc.pages[0].text.casefold()
        scores = {k: sum(first_page.count(p.casefold()) for p in phrases[k]) for k in DOCUMENTS}
        best = max(scores.values())
        kinds = [k for k, score in scores.items() if score == best]
        name = Path(doc.path).name
        if best == 0 or len(kinds) > 1:
            unknown.append(name)
            continue
        found[kinds[0]].append(name)
        by_kind[kinds[0]] = doc
    problems = [f"cannot tell what {name} is" for name in unknown]
    for kind, names in found.items():
        if len(names) != 1:
            problems.append(f"{DOCUMENT_NAMES[kind]}: {', '.join(names) or 'none'}")
    if problems:
        raise ExtractionError(
            "cannot tell which PDF is the B/L, the commercial invoice and the eManifest ("
            + "; ".join(problems)
            + "); a person needs to check, or add the title to "
            + "src/crews/extraction/config/documents.toml"
        )
    return by_kind


def render(doc: PdfText) -> str:
    """Document text for a prompt: each page's text, then its tables one row per line."""
    parts = []
    for page in doc.pages:
        parts.append(f"=== page {page.number} ===\n{page.text.strip()}")
        for number, table in enumerate(page.tables, start=1):
            rows = "\n".join(" | ".join(row) for row in table.rows)
            parts.append(f"--- table {number} on page {page.number} ---\n{rows}")
    return "\n\n".join(parts)


# --- the crew ---------------------------------------------------------------------------


def build_crew(
    by_kind: dict[str, PdfText], llm: BaseLLM | str, max_retries: int
) -> tuple[Crew, dict[str, Task]]:
    agents_text = load_config("agents.yaml")
    tasks_text = load_config("tasks.yaml")
    rules = tasks_text["shared"]["rules"]

    def agent(name: str) -> Agent:
        return Agent(**agents_text[name], llm=llm, allow_delegation=False, tools=[], verbose=False)

    tasks: dict[str, Task] = {}
    for name, (kind, model, check) in SPECIALISTS.items():
        doc = by_kind[kind]
        pages = len(doc.pages)
        text = tasks_text[name]
        tasks[name] = Task(
            name=name,
            # str.replace, not format: the document may contain braces.
            description=text["description"]
            .replace("{rules}", rules)
            .replace("{document}", render(doc)),
            expected_output=text["expected_output"],
            agent=agent(name),  # one agent per parallel task: CrewAI cannot share one
            output_pydantic=model,
            async_execution=True,
            guardrail=guardrails.make_guardrail(
                model, lambda parsed, check=check, pages=pages: check(parsed, pages)
            ),
            guardrail_max_retries=max_retries,
        )

    def readings() -> dict[str, BaseModel]:
        return {kind: tasks[kind].output.pydantic for kind in DOCUMENTS}

    text = tasks_text["reconciler"]
    tasks["reconciler"] = Task(
        name="reconciler",
        description=text["description"] + "\n\nThe readings are given in this order: bl, ci, emf.",
        expected_output=text["expected_output"],
        agent=agent("reconciler"),
        context=[tasks[kind] for kind in DOCUMENTS],
        output_pydantic=Reconciled,
        guardrail=guardrails.make_guardrail(
            Reconciled, lambda rec: guardrails.check_reconciled(rec, readings())
        ),
        guardrail_max_retries=max_retries,
    )
    crew = Crew(
        agents=[t.agent for t in tasks.values()],
        tasks=list(tasks.values()),
        process=Process.sequential,
        memory=False,
        verbose=False,
    )
    if not isinstance(crew._task_output_handler.storage, _NoTaskOutputStorage):
        # A CrewAI upgrade moved the storage; stop rather than write prompts to disk.
        raise RuntimeError("CrewAI's task output storage is not switched off (see crew.py)")
    return crew, tasks


def extract_shipment(
    documents: list[PdfText], settings: dict, *, llm: BaseLLM | None = None
) -> Shipment:
    by_kind = identify(documents, document_phrases())
    model = llm or LLM(model=settings["models"]["extraction"], temperature=0)
    crew, tasks = build_crew(by_kind, model, settings["crews"]["guardrail_max_retries"])
    try:
        crew.kickoff()
    except Exception as e:
        if "guardrail" in str(e):
            # The model kept giving answers the format checks reject.
            raise ExtractionError(f"extraction failed its format checks: {e}") from e
        raise
    _record_usage(crew)
    out = {name: task.output.pydantic for name, task in tasks.items()}
    return build_shipment(out["bl"], out["ci"], out["ci_lines"], out["emf"], out["reconciler"])


def make_extractor(settings: dict):
    """The `extractor` for ShipmentFlow."""
    return lambda documents: extract_shipment(documents, settings)


def _record_usage(crew: Crew) -> None:
    """Token counts on the run trace's extract span (numbers only, for issue #13)."""
    usage = crew.usage_metrics
    if usage is None:
        return
    span = trace.get_current_span()
    span.set_attribute("prompt_tokens", usage.prompt_tokens)
    span.set_attribute("completion_tokens", usage.completion_tokens)
    span.set_attribute("model_requests", usage.successful_requests)


# --- readings -> Shipment (code, no model) ------------------------------------------


def _field(reading: Reading, source: SpecialistDocument) -> HeaderField:
    return HeaderField(value=reading.value, source=source, page=reading.page)


def build_shipment(
    bl: BLReading, ci: CIReading, ci_lines: CILines, emf: EMFReading, rec: Reconciled
) -> Shipment:
    readings: dict[SpecialistDocument, BaseModel] = {"bl": bl, "ci": ci, "emf": emf}
    header: dict[str, HeaderField] = {}
    for kind, model in readings.items():
        for name, value in model:
            # The shared fields are filled below, from every document that gives them.
            if (
                isinstance(value, Reading)
                and name in Header.model_fields
                and name not in SHARED_FIELDS
            ):
                header[name] = _field(value, kind)
    for name, documents in SHARED_FIELDS.items():
        given = {doc: getattr(readings[doc], name) for doc in documents}
        given = {doc: r for doc, r in given.items() if r.value != ""}
        choice = getattr(rec, name)
        if choice is not None and choice.source in given:
            source, value = choice.source, choice.value
        elif given:
            source = next(iter(given))  # SHARED_FIELDS order: the B/L first
            value = given[source].value
        else:
            source, value = documents[0], ""
        header[name] = HeaderField(
            value=value,
            source=source,
            found={doc: r.value for doc, r in given.items()},
            page=given[source].page if source in given else None,
        )
    return Shipment(
        header=Header(**header),
        invoice_total=_field(ci.invoice_total, "ci"),
        lines=ci_lines.lines,
    )
