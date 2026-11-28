"""Format checks on what each extraction task returns.

A CrewAI guardrail gets the task output and returns (True, result) or (False, reason).
On False the agent sees the reason and tries again, up to `guardrail_max_retries` times
([crews] in settings.toml). Only problems a second reading can fix are checked here:
JSON that does not fit the model, dates and numbers in the wrong form, page numbers that
do not exist, missing required fields. Whether the documents agree is the validator's job.
"""

import datetime as dt
import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from src.crews.extraction.outputs import (
    SHARED_FIELDS,
    BLReading,
    CILines,
    CIReading,
    EMFReading,
    Reading,
    Reconciled,
)

GuardrailResult = tuple[bool, Any]
Check = Callable[[Any], list[str]]

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def parse(model: type[BaseModel], raw: str) -> BaseModel:
    """The task's text as `model`. Models sometimes wrap JSON in a ``` fence."""
    match = _FENCE.match(raw)
    return model.model_validate_json(match.group(1) if match else raw)


def make_guardrail(model: type[BaseModel], check: Check) -> Callable[[Any], GuardrailResult]:
    def guardrail(output: Any) -> GuardrailResult:
        try:
            parsed = parse(model, output.raw)
        except ValidationError as e:
            return False, f"The answer does not match the required JSON format: {e}"
        problems = check(parsed)
        if problems:
            return False, "Fix these and answer again:\n- " + "\n- ".join(problems)
        return True, parsed

    return guardrail


def _readings(model: BaseModel) -> dict[str, Reading]:
    return {name: value for name, value in model if isinstance(value, Reading)}


def check_pages(model: BaseModel, pages: int) -> list[str]:
    """Every value needs the page it is on, and the page must exist. "" needs no page."""
    given = {name: r for name, r in _readings(model).items() if r.value != ""}
    no_page = [
        f"{name}: give the page this value is on" for name, r in given.items() if r.page is None
    ]
    past_the_end = [
        f"{name}: page {r.page}, but the document has {pages} page(s)"
        for name, r in given.items()
        if r.page is not None and r.page > pages
    ]
    return no_page + past_the_end


def check_date(name: str, reading: Reading, *, time_allowed: bool = False) -> list[str]:
    if reading.value == "":
        return []
    text = str(reading.value)
    formats = ["%Y-%m-%d", "%Y-%m-%dT%H:%M"] if time_allowed else ["%Y-%m-%d"]
    if any(_is_date(text, fmt) for fmt in formats):
        return []
    form = "YYYY-MM-DD or YYYY-MM-DDTHH:MM" if time_allowed else "YYYY-MM-DD"
    return [f"{name}: {text!r} is not a date in the form {form}"]


def _is_date(text: str, fmt: str) -> bool:
    """True if `text` is a real date written exactly in `fmt` (zero-padded, nothing extra).

    strptime alone also takes 2026-1-5; formatting the parsed date back and comparing
    rejects that, and 20261123 and 2026-02-30 fail to parse at all.
    """
    try:
        return dt.datetime.strptime(text, fmt).strftime(fmt) == text
    except ValueError:
        return False


def check_number(name: str, reading: Reading) -> list[str]:
    if reading.value == "" or isinstance(reading.value, int | float):
        return []
    return [f"{name}: {reading.value!r} must be a JSON number, without units or separators"]


def check_required(name: str, reading: Reading) -> list[str]:
    if str(reading.value).strip() == "":
        return [f"{name} is missing; every document of this type has one, read it again"]
    return []


def check_bl(bl: BLReading, pages: int) -> list[str]:
    return [
        *check_pages(bl, pages),
        *check_required("bl_number", bl.bl_number),
        *check_number("packages", bl.packages),
        *check_number("gross_weight", bl.gross_weight),
    ]


def check_ci(ci: CIReading, pages: int) -> list[str]:
    problems = [
        *check_pages(ci, pages),
        *check_required("invoice_number", ci.invoice_number),
        *check_date("invoice_date", ci.invoice_date),
        *check_number("invoice_total", ci.invoice_total),
    ]
    currency = str(ci.currency.value)
    if currency and not re.fullmatch(r"[A-Z]{3}", currency):
        problems.append(f"currency: {currency!r} must be a three-letter ISO code such as USD")
    return problems


def check_ci_lines(ci: CILines, pages: int) -> list[str]:
    if not ci.lines:
        return ["lines: no invoice lines; list every line of the invoice table"]
    numbers = [line.line_no for line in ci.lines]
    numbering = (
        [f"lines: number the lines 1, 2, 3, ... in order (got {numbers})"]
        if numbers != list(range(1, len(numbers) + 1))
        else []
    )
    countries = [
        f"line {line.line_no}: country_of_origin {line.country_of_origin!r} must be a "
        'two-letter ISO code such as CN, or "" if the invoice does not give it'
        for line in ci.lines
        if line.country_of_origin and not re.fullmatch(r"[A-Z]{2}", line.country_of_origin)
    ]
    return numbering + countries


def check_emf(emf: EMFReading, pages: int) -> list[str]:
    return [
        *check_pages(emf, pages),
        *check_required("cargo_control_number", emf.cargo_control_number),
        *check_date("eta", emf.eta, time_allowed=True),
        *check_number("packages", emf.packages),
        *check_number("gross_weight", emf.gross_weight),
    ]


def check_reconciled(rec: Reconciled, readings: dict[str, BaseModel]) -> list[str]:
    """A pick must come from a document that gave the field, with that document's value."""
    picks = dict(rec)
    fields = {kind: dict(reading) for kind, reading in readings.items()}
    chosen = {name: picks[name] for name in SHARED_FIELDS if picks[name] is not None}
    wrong_document = [
        f"{name}: {choice.source} does not give this field"
        for name, choice in chosen.items()
        if choice.source not in SHARED_FIELDS[name]
    ]
    no_value = [
        f"{name}: you picked {choice.source}, but it does not give a value"
        for name, choice in chosen.items()
        if choice.source in SHARED_FIELDS[name] and fields[choice.source][name].value == ""
    ]
    changed_value = [
        f"{name}: {choice.source} says {fields[choice.source][name].value!r}, not "
        f"{choice.value!r}; copy the value from the document you pick"
        for name, choice in chosen.items()
        if choice.source in SHARED_FIELDS[name]
        and fields[choice.source][name].value not in ("", choice.value)
    ]
    return wrong_document + no_value + changed_value
