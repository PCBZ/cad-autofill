"""validator.py: a passing shipment and each failure type."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError

from src.schema import Shipment
from src.validator import validate

TOLERANCE = 0.01


def make_shipment(**changes) -> dict:
    """A shipment that passes every check. `changes` replaces top-level keys."""
    data = {
        "header": {
            "bl_number": {
                "value": "MAEU123456789",
                "source": "bl",
                "found": {"bl": "MAEU123456789", "ci": "MAEU 123456789", "emf": "maeu123456789"},
            },
            "packages": {"value": 20, "source": "bl", "found": {"bl": 20, "emf": 20.0}},
            "gross_weight": {"value": 1240, "source": "bl", "found": {"bl": 1240, "emf": 1240.0}},
            "consignee": {
                "value": "Acme Co., Ltd.",
                "source": "bl",
                "found": {"bl": "Acme Co., Ltd.", "ci": "ACME CO LTD"},
            },
            "currency": {"value": "USD", "source": "ci"},
        },
        "invoice_total": {"value": 2500.0, "source": "ci"},
        "lines": [
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
        ],
    }
    data.update(changes)
    return data


def run(data: dict):
    return validate(Shipment.model_validate(data), amount_tolerance=TOLERANCE)


def with_header(field: str, **changes) -> dict:
    data = copy.deepcopy(make_shipment())
    data["header"][field].update(changes)
    return data


class PassingTests(unittest.TestCase):
    def test_clean_shipment_passes(self):
        result = run(make_shipment())
        self.assertTrue(result.passed)
        self.assertEqual(result.issues, [])

    def test_validate_does_not_change_the_shipment(self):
        shipment = Shipment.model_validate(make_shipment())
        before = shipment.model_dump()
        validate(shipment, amount_tolerance=TOLERANCE)
        self.assertEqual(shipment.model_dump(), before)

    def test_fields_given_by_one_document_are_not_compared(self):
        data = with_header("packages", found={"bl": 20})
        self.assertTrue(run(data).passed)


class TotalTests(unittest.TestCase):
    def test_sum_differs_from_total(self):
        result = run(
            make_shipment(
                invoice_total={"value": 2500.0, "source": "ci"},
                lines=[
                    {**make_shipment()["lines"][0]},
                    {**make_shipment()["lines"][1], "amount": 2230.0},
                ],
            )
        )
        self.assertFalse(result.passed)
        (issue,) = result.issues
        self.assertEqual(issue.field, "invoice_total")
        self.assertEqual(issue.issue, "sum of lines 2,480.00 does not equal invoice total 2,500.00")
        self.assertEqual(issue.found, {"lines_sum": 2480.0, "ci": 2500.0})

    def test_difference_within_tolerance_passes(self):
        data = make_shipment(invoice_total={"value": 2500.01, "source": "ci"})
        self.assertTrue(run(data).passed)

    def test_difference_just_over_tolerance_fails(self):
        data = make_shipment(invoice_total={"value": 2500.02, "source": "ci"})
        self.assertFalse(run(data).passed)

    def test_float_noise_does_not_fail(self):
        line = {**make_shipment()["lines"][0], "amount": 0.1}
        data = make_shipment(
            lines=[{**line, "line_no": n} for n in (1, 2, 3)],
            invoice_total={"value": 0.3, "source": "ci"},
        )
        self.assertTrue(run(data).passed)

    def test_total_given_as_text_with_commas(self):
        data = make_shipment(invoice_total={"value": "2,500.00", "source": "ci"})
        self.assertTrue(run(data).passed)

    def test_total_missing(self):
        data = make_shipment()
        del data["invoice_total"]
        (issue,) = run(data).issues
        self.assertEqual((issue.field, issue.issue), ("invoice_total", "invoice total is missing"))

    def test_total_empty_string_counts_as_missing(self):
        data = make_shipment(invoice_total={"value": "", "source": "ci"})
        self.assertEqual(run(data).issues[0].issue, "invoice total is missing")

    def test_total_not_a_number(self):
        data = make_shipment(invoice_total={"value": "see attached", "source": "ci"})
        (issue,) = run(data).issues
        self.assertEqual(issue.field, "invoice_total")
        self.assertIn("not a number", issue.issue)

    def test_no_lines(self):
        (issue,) = run(make_shipment(lines=[])).issues
        self.assertEqual(issue.field, "lines")


class AgreementTests(unittest.TestCase):
    def test_gross_weight_disagrees(self):
        data = with_header("gross_weight", found={"bl": 12400, "emf": 1240})
        result = run(data)
        self.assertFalse(result.passed)
        (issue,) = result.issues
        self.assertEqual(issue.field, "header.gross_weight")
        self.assertEqual(issue.issue, "B/L and EMF disagree")
        self.assertEqual(issue.found, {"bl": 12400, "emf": 1240})

    def test_each_cross_checked_field_is_checked(self):
        bad = {
            "bl_number": {"bl": "MAEU123456789", "emf": "MAEU999999999"},
            "packages": {"bl": 20, "emf": 2},
            "gross_weight": {"bl": 1240, "emf": 124},
            "consignee": {"bl": "Acme Co., Ltd.", "ci": "Beta Trading Inc"},
        }
        for name, found in bad.items():
            with self.subTest(field=name):
                (issue,) = run(with_header(name, found=found)).issues
                self.assertEqual(issue.field, f"header.{name}")

    def test_long_identifiers_that_differ_by_one_disagree(self):
        data = with_header("bl_number", found={"bl": "1234567890", "emf": "1234567891"})
        self.assertEqual([i.field for i in run(data).issues], ["header.bl_number"])

    def test_numeric_looking_text_still_matches_for_numbers(self):
        data = with_header("packages", found={"bl": "20", "emf": 20})
        self.assertTrue(run(data).passed)

    def test_number_with_a_unit_is_compared_as_text(self):
        data = with_header("gross_weight", found={"bl": "1240 KGS", "emf": 1240})
        self.assertFalse(run(data).passed)

    def test_human_reading_cannot_be_in_found(self):
        with self.assertRaises(ValidationError):
            Shipment.model_validate(with_header("bl_number", found={"bl": "A", "human": "B"}))

    def test_three_documents_listed_in_the_message(self):
        data = with_header("bl_number", found={"bl": "A1", "ci": "A1", "emf": "B2"})
        (issue,) = run(data).issues
        self.assertEqual(issue.issue, "B/L, CI and EMF disagree")
        self.assertEqual(issue.found, {"bl": "A1", "ci": "A1", "emf": "B2"})

    def test_case_spacing_and_punctuation_do_not_count(self):
        data = with_header("consignee", found={"bl": "Acme Co., Ltd.", "ci": "  ACME   co ltd "})
        self.assertTrue(run(data).passed)

    def test_number_as_text_matches_number(self):
        data = with_header("gross_weight", found={"bl": "1,240.00", "emf": 1240})
        self.assertTrue(run(data).passed)

    def test_document_that_did_not_give_the_field_is_ignored(self):
        data = with_header("packages", found={"bl": 20, "ci": "", "emf": 20})
        self.assertTrue(run(data).passed)

    def test_value_typed_by_a_person_is_not_compared_again(self):
        data = with_header(
            "gross_weight", source="human", value=1240, found={"bl": 12400, "emf": 1240}
        )
        self.assertTrue(run(data).passed)

    def test_two_failures_are_both_reported(self):
        data = with_header("packages", found={"bl": 20, "emf": 2})
        data["header"]["gross_weight"]["found"] = {"bl": 12400, "emf": 1240}
        data["invoice_total"] = {"value": 9999.0, "source": "ci"}
        self.assertEqual(
            [i.field for i in run(data).issues],
            ["invoice_total", "header.packages", "header.gross_weight"],
        )


class AcceptanceTests(unittest.TestCase):
    ACCEPTED = {
        "field": "invoice_total",
        "action": "accepted",
        "values": {"lines_sum": 2480.0, "ci": 2500.0},
        "by": "human",
        "note": "freight 20.00 is on the invoice total but not a line",
    }

    def short_shipment(self, review: list) -> dict:
        lines = make_shipment()["lines"]
        lines[1]["amount"] = 2230.0
        return make_shipment(lines=lines, review=review)

    def test_accepted_issue_stays_listed_but_does_not_block(self):
        result = run(self.short_shipment([self.ACCEPTED]))
        self.assertTrue(result.passed)
        (issue,) = result.issues
        self.assertTrue(issue.accepted)
        self.assertEqual(result.open_issues, [])

    def test_acceptance_for_other_values_does_not_hold(self):
        stale = {**self.ACCEPTED, "values": {"lines_sum": 2490.0, "ci": 2500.0}}
        result = run(self.short_shipment([stale]))
        self.assertFalse(result.passed)
        self.assertEqual(len(result.open_issues), 1)

    def test_acceptance_reopens_when_a_large_value_changes_slightly(self):
        found = {"bl": 1234567890, "ci": 9999999999}
        data = with_header("packages", found=found)
        accepted = {
            "field": "header.packages",
            "action": "accepted",
            "values": found,
            "by": "human",
            "note": "ok",
        }
        data["review"] = [accepted]
        self.assertTrue(run(data).passed)
        data["header"]["packages"]["found"] = {"bl": 1234567891, "ci": 9999999999}
        result = run(data)
        self.assertFalse(result.passed)
        self.assertEqual(len(result.open_issues), 1)

    def test_acceptance_for_another_field_does_not_hold(self):
        other = {**self.ACCEPTED, "field": "header.gross_weight"}
        self.assertFalse(run(self.short_shipment([other])).passed)

    def test_accepted_and_open_issues_together(self):
        data = self.short_shipment([self.ACCEPTED])
        data["header"]["packages"]["found"] = {"bl": 20, "emf": 2}
        result = run(data)
        self.assertFalse(result.passed)
        self.assertEqual([i.field for i in result.open_issues], ["header.packages"])
        self.assertEqual(len(result.issues), 2)

    def test_correction_record_is_not_an_acceptance(self):
        corrected = {
            "field": "invoice_total",
            "action": "corrected",
            "old": 2500.0,
            "new": 2480.0,
            "by": "human",
        }
        self.assertFalse(run(self.short_shipment([corrected])).passed)


if __name__ == "__main__":
    unittest.main()
