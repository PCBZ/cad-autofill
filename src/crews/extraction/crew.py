"""The extraction crew: B/L, CI and EMF specialists in parallel, then a reconciler.

    documents -> which PDF is which (code) -> bl, ci, ci_lines, emf tasks (async)
              -> reconciler (bl, ci, emf) -> build_shipment (code) -> Shipment

Agent and task text is in config/agents.yaml and config/tasks.yaml next to this file (the
CrewAI convention). Settings are TOML: the title phrases that tell the PDFs apart are in
config/documents.toml, and the model is [models] extraction in settings.toml.
`make_extractor(settings)` gives the function ShipmentFlow calls in its extract step.

The crew sees the document text only. It has no tools, no memory and no delegation.
"""

import functools
import tomllib
from collections.abc import Callable
from pathlib import Path

import yaml
from crewai import LLM, Agent, Crew, Process, Task
from crewai.llms.base_llm import BaseLLM
from opentelemetry import trace
from pydantic import BaseModel

from src.crews.extraction import guardrails
from src.crews.extraction.outputs import (
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
# CrewAI's own default, used when [crews] guardrail_max_retries is not set.
DEFAULT_MAX_RETRIES: int = Task.model_fields["guardrail_max_retries"].default
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
    kind_of = {Path(doc.path).name: _kind_of(doc, phrases) for doc in documents}
    unknown = [name for name, kind in kind_of.items() if kind is None]
    found = {kind: [name for name, k in kind_of.items() if k == kind] for kind in DOCUMENTS}
    problems = [f"cannot tell what {name} is" for name in unknown] + [
        f"{DOCUMENT_NAMES[kind]}: {', '.join(names) or 'none'}"
        for kind, names in found.items()
        if len(names) != 1
    ]
    if problems:
        raise ExtractionError(
            "cannot tell which PDF is the B/L, the commercial invoice and the eManifest ("
            + "; ".join(problems)
            + "); a person needs to check, or add the title to "
            + "src/crews/extraction/config/documents.toml"
        )
    return {kind_of[Path(doc.path).name]: doc for doc in documents}


def _kind_of(doc: PdfText, phrases: dict[str, list[str]]) -> str | None:
    """The type whose title phrases occur most on page 1; None if no phrase or a tie."""
    first_page = doc.pages[0].text.casefold()
    scores = {k: sum(first_page.count(p.casefold()) for p in phrases[k]) for k in DOCUMENTS}
    best = max(scores.values())
    kinds = [kind for kind, score in scores.items() if score == best]
    return kinds[0] if best > 0 and len(kinds) == 1 else None


def prompt_text(doc: PdfText) -> str:
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
    by_kind: dict[str, PdfText], llm: BaseLLM | str, max_retries: int = DEFAULT_MAX_RETRIES
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
            .replace("{document}", prompt_text(doc)),
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
    return crew, tasks


def extract_shipment(
    documents: list[PdfText], settings: dict, *, llm: BaseLLM | None = None
) -> Shipment:
    by_kind = identify(documents, document_phrases())
    model = llm or LLM(model=settings["models"]["extraction"], temperature=0)
    max_retries = settings.get("crews", {}).get("guardrail_max_retries", DEFAULT_MAX_RETRIES)
    crew, tasks = build_crew(by_kind, model, max_retries)
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


def make_extractor(settings: dict) -> Callable[[list[PdfText]], Shipment]:
    """The `extractor` ShipmentFlow calls with the PDFs: extract_shipment, settings filled in."""
    return functools.partial(extract_shipment, settings=settings)


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
    # Each reading as a plain {field name: value} dict, so fields are looked up by name.
    fields: dict[SpecialistDocument, dict] = {"bl": dict(bl), "ci": dict(ci), "emf": dict(emf)}
    picks = dict(rec)
    header: dict[str, HeaderField] = {
        # The shared fields are filled below, from every document that gives them.
        name: _field(value, kind)
        for kind, values in fields.items()
        for name, value in values.items()
        if isinstance(value, Reading) and name in Header.model_fields and name not in SHARED_FIELDS
    }
    for name, documents in SHARED_FIELDS.items():
        given = {doc: fields[doc][name] for doc in documents if fields[doc][name].value != ""}
        choice = picks[name]
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
