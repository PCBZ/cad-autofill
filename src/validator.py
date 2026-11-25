"""Validator: plain code, no model. Two kinds of check:

* the sum of the invoice lines equals the invoice total, within a tolerance;
* B/L number, packages, gross weight and consignee agree across the documents that
  gave them (`HeaderField.found`): text fields as text, numbers as numbers.

`validate` returns a `Validation`. An issue a person accepted at review (a
`ReviewAccepted` record for the same field and the same values) stays in the report
but does not stop the run. If a later correction changes the values, the issue no
longer matches its acceptance and is open again.
"""

import math
from collections.abc import Callable

from src.schema import (
    Document,
    ReviewAccepted,
    Scalar,
    Shipment,
    Validation,
    ValidationIssue,
)

DOCUMENT_NAMES: dict[Document, str] = {"bl": "B/L", "ci": "CI", "emf": "EMF", "email": "email"}


def validate(shipment: Shipment, *, amount_tolerance: float) -> Validation:
    issues = [
        *_check_total(shipment, amount_tolerance),
        *_check_agreement(shipment),
    ]
    accepted = [r for r in shipment.review if isinstance(r, ReviewAccepted)]
    for issue in issues:
        # Exact match: if any reviewed value changed, the acceptance no longer holds.
        issue.accepted = any(r.field == issue.field and r.values == issue.found for r in accepted)
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
    for name, field in shipment.header:
        agree = AGREEMENT_RULES.get(name)
        # A value typed by a person is the final word; it is not compared again.
        if agree is None or field is None or field.source == "human":
            continue
        readings = {doc: v for doc, v in field.found.items() if v != ""}
        if len(readings) < 2 or agree(list(readings.values())):
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


def _same_text(values: list[Scalar]) -> bool:
    """Case, spacing and punctuation do not count as a difference."""
    return len({"".join(ch for ch in str(v).casefold() if ch.isalnum()) for v in values}) == 1


def _same_number(values: list[Scalar]) -> bool:
    """`1,240.00` equals 1240. Text that is not a number is compared as text."""
    numbers = [_number(v) for v in values]
    if any(n is None for n in numbers):
        return _same_text(values)
    return all(math.isclose(n, numbers[0], rel_tol=1e-9, abs_tol=1e-9) for n in numbers)


# Header fields that more than one document gives, and how their readings are compared.
# Identifiers and names are text: a B/L number is never "close" to another one.
AGREEMENT_RULES: dict[str, Callable[[list[Scalar]], bool]] = {
    "bl_number": _same_text,
    "consignee": _same_text,
    "packages": _same_number,
    "gross_weight": _same_number,
}
