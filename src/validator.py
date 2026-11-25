"""Validator: plain code, no model. Two kinds of check:

* the sum of the invoice lines equals the invoice total, within a tolerance;
* B/L number, packages, gross weight and consignee agree across the documents that
  gave them (`HeaderField.found`).

`validate` returns a `Validation`. An issue a person accepted at review (a
`ReviewAccepted` record for the same field and the same values) stays in the report
but does not stop the run. If a later correction changes the values, the issue no
longer matches its acceptance and is open again.
"""

import math

from src.schema import (
    ReviewAccepted,
    Scalar,
    Shipment,
    Source,
    Validation,
    ValidationIssue,
)

# Header fields that more than one document gives, so the documents can be compared.
CROSS_CHECKED = ("bl_number", "packages", "gross_weight", "consignee")

DOCUMENT_NAMES: dict[Source, str] = {"bl": "B/L", "ci": "CI", "emf": "EMF", "email": "email"}


def validate(shipment: Shipment, *, amount_tolerance: float) -> Validation:
    issues = [
        *_check_total(shipment, amount_tolerance),
        *_check_agreement(shipment),
    ]
    accepted = [r for r in shipment.review if isinstance(r, ReviewAccepted)]
    for issue in issues:
        issue.accepted = any(
            r.field == issue.field and _same_found(r.values, issue.found) for r in accepted
        )
    return Validation(passed=all(i.accepted for i in issues), issues=issues)


def _check_total(shipment: Shipment, tolerance: float) -> list[ValidationIssue]:
    if not shipment.lines:
        return [ValidationIssue(field="lines", issue="no invoice lines were extracted")]
    total = shipment.invoice_total
    if total is None or total.value == "":
        return [ValidationIssue(field="invoice_total", issue="invoice total is missing")]
    total_value = _number(total.value)
    if total_value is None:
        return [
            ValidationIssue(
                field="invoice_total",
                issue=f"invoice total {total.value!r} is not a number",
                found={"ci": total.value},
            )
        ]
    lines_sum = math.fsum(line.amount for line in shipment.lines)
    if abs(lines_sum - total_value) <= tolerance + 1e-9:
        return []
    return [
        ValidationIssue(
            field="invoice_total",
            issue=(
                f"sum of lines {lines_sum:,.2f} does not equal invoice total {total_value:,.2f}"
            ),
            found={"lines_sum": round(lines_sum, 4), "ci": total_value},
        )
    ]


def _check_agreement(shipment: Shipment) -> list[ValidationIssue]:
    issues = []
    for name in CROSS_CHECKED:
        field = getattr(shipment.header, name)
        # A value typed by a person is the final word; it is not compared again.
        if field is None or field.source == "human":
            continue
        readings = {doc: v for doc, v in field.found.items() if v != ""}
        if len(readings) < 2 or _agree(list(readings.values())):
            continue
        names = [DOCUMENT_NAMES[doc] for doc in readings]
        issues.append(
            ValidationIssue(
                field=f"header.{name}",
                issue=f"{', '.join(names[:-1])} and {names[-1]} disagree",
                found=dict(readings),
            )
        )
    return issues


def _number(value: Scalar) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def _text(value: Scalar) -> str:
    """Case, spacing and punctuation do not count as a difference."""
    return "".join(ch for ch in str(value).casefold() if ch.isalnum())


def _agree(values: list[Scalar]) -> bool:
    numbers = [_number(v) for v in values]
    if all(n is not None for n in numbers):
        return all(math.isclose(n, numbers[0], rel_tol=1e-9, abs_tol=1e-9) for n in numbers)
    return len({_text(v) for v in values}) == 1


def _same_found(accepted: dict[str, Scalar | None], found: dict[str, Scalar | None]) -> bool:
    if accepted.keys() != found.keys():
        return False
    for key, a in accepted.items():
        b = found[key]
        if isinstance(a, int | float) and isinstance(b, int | float):
            if not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9):
                return False
        elif a != b:
            return False
    return True
