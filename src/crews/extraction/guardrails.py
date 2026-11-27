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

from src.crews.extraction.models import (
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
    problems = []
    for name, reading in _readings(model).items():
        if reading.value != "" and reading.page is not None and reading.page > pages:
            problems.append(f"{name}: page {reading.page}, but the document has {pages} page(s)")
    return problems


def check_date(name: str, reading: Reading, *, time_allowed: bool = False) -> list[str]:
    if reading.value == "":
        return []
    text = str(reading.value)
    try:
        if time_allowed and "T" in text:
            dt.datetime.strptime(text, "%Y-%m-%dT%H:%M")
        else:
            dt.date.fromisoformat(text)
    except ValueError:
        form = "YYYY-MM-DD or YYYY-MM-DDTHH:MM" if time_allowed else "YYYY-MM-DD"
        return [f"{name}: {text!r} is not a date in the form {form}"]
    if len(text) < 10:  # fromisoformat also takes 20261123
        return [f"{name}: write the date as YYYY-MM-DD"]
    return []


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
    problems = []
    if not ci.lines:
        problems.append("lines: no invoice lines; list every line of the invoice table")
    numbers = [line.line_no for line in ci.lines]
    if numbers != list(range(1, len(numbers) + 1)):
        problems.append(f"lines: number the lines 1, 2, 3, ... in order (got {numbers})")
    for line in ci.lines:
        country = line.country_of_origin
        if country and not re.fullmatch(r"[A-Z]{2}", country):
            problems.append(
                f"line {line.line_no}: country_of_origin {country!r} must be a two-letter "
                'ISO code such as CN, or "" if the invoice does not give it'
            )
    return problems


def check_emf(emf: EMFReading, pages: int) -> list[str]:
    return [
        *check_pages(emf, pages),
        *check_required("cargo_control_number", emf.cargo_control_number),
        *check_date("eta", emf.eta, time_allowed=True),
        *check_number("packages", emf.packages),
        *check_number("gross_weight", emf.gross_weight),
    ]


def check_reconciled(rec: Reconciled, readings: dict[str, BaseModel]) -> list[str]:
    """A pick must come from a document that gave the field."""
    problems = []
    for name, documents in SHARED_FIELDS.items():
        choice = getattr(rec, name)
        if choice is None:
            continue
        if choice.source not in documents:
            problems.append(f"{name}: {choice.source} does not give this field")
            continue
        reading = getattr(readings[choice.source], name)
        if reading.value == "":
            problems.append(f"{name}: you picked {choice.source}, but it does not give a value")
    return problems
