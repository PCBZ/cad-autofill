"""schema.py models against the examples in the design doc (wiki: Design, "Data
structure" and "Human review of validation failures")."""

import json
import sys
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError

from src.schema import CorrectionsFile, Correction, Shipment, ShipmentState

SHIPMENT_JSON = """
{
  "email": {"message_id": "18c2f0a9d3e4b5c6", "thread_id": "18c2f0a9d3e4b5c6",
            "from": "docs@client.example", "subject": "", "received": "2026-11-23T14:05:00Z"},
  "header": {
    "bl_number": {"value": "MAEU123456789", "source": "bl"},
    "cargo_control_number": {"value": "", "source": "emf"},
    "currency": {"value": "USD", "source": "ci"}
  },
  "lines": [
    {"line_no": 1, "description": "", "hs_code": "", "country_of_origin": "CN",
     "quantity": 100, "unit": "PCS", "unit_price": 2.5, "amount": 250.0}
  ],
  "validation": {"passed": true, "issues": []},
  "review": [
    {"field": "header.gross_weight", "action": "corrected", "old": 12400, "new": 1240,
     "by": "human", "note": "B/L says 1,240.00 KGS; extraction misread it"},
    {"field": "invoice_total", "action": "accepted", "values": {"lines_sum": 2480.0, "ci": 2500.0},
     "by": "human", "note": "freight 20.00 is on the invoice total but not a line"}
  ]
}
"""

CORRECTIONS_TOML = """
[[correction]]
field = "header.gross_weight"
issue = "B/L and EMF disagree"
found = { bl = 12400, emf = 1240 }
value = 1240
note = "B/L says 1,240.00 KGS; extraction misread it"

[[correction]]
field = "invoice_total"
issue = "sum of lines 2,480.00 does not equal invoice total 2,500.00"
found = { lines_sum = 2480.0, ci = 2500.0 }
accept = true
note = "freight 20.00 is on the invoice total but not a line"
"""


class ShipmentTests(unittest.TestCase):
    def test_design_example_validates(self):
        shipment = Shipment.model_validate_json(SHIPMENT_JSON)
        self.assertEqual(shipment.email.sender, "docs@client.example")
        self.assertEqual(shipment.header.bl_number.source, "bl")
        self.assertEqual(shipment.header.cargo_control_number.value, "")
        self.assertIsNone(shipment.header.eta)
        self.assertEqual(shipment.lines[0].quantity, 100)
        self.assertEqual([r.action for r in shipment.review], ["corrected", "accepted"])
        self.assertEqual(shipment.review[0].new, 1240)
        self.assertEqual(shipment.review[1].values["ci"], 2500.0)

    def test_round_trip_keeps_json_names(self):
        shipment = Shipment.model_validate_json(SHIPMENT_JSON)
        dumped = json.loads(shipment.model_dump_json(by_alias=True, exclude_none=True))
        self.assertEqual(dumped["email"]["from"], "docs@client.example")
        self.assertEqual(Shipment.model_validate(dumped), shipment)

    def test_empty_shipment_is_valid(self):
        self.assertEqual(Shipment().lines, [])

    def test_unknown_source_rejected(self):
        data = json.loads(SHIPMENT_JSON)
        data["header"]["currency"]["source"] = "invoice"
        with self.assertRaises(ValidationError):
            Shipment.model_validate(data)

    def test_misspelled_header_field_rejected(self):
        data = json.loads(SHIPMENT_JSON)
        data["header"]["bl_numbr"] = {"value": "X", "source": "bl"}
        with self.assertRaises(ValidationError):
            Shipment.model_validate(data)

    def test_review_action_selects_model(self):
        data = json.loads(SHIPMENT_JSON)
        del data["review"][0]["new"]  # a "corrected" record needs the new value
        with self.assertRaises(ValidationError):
            Shipment.model_validate(data)

    def test_accepted_review_needs_note(self):
        data = json.loads(SHIPMENT_JSON)
        del data["review"][1]["note"]
        with self.assertRaises(ValidationError):
            Shipment.model_validate(data)


class CorrectionsTests(unittest.TestCase):
    def test_design_example_validates(self):
        corrections = CorrectionsFile.model_validate(tomllib.loads(CORRECTIONS_TOML))
        first, second = corrections.correction
        self.assertEqual(first.value, 1240)
        self.assertEqual(first.found, {"bl": 12400, "emf": 1240})
        self.assertTrue(second.accept)
        self.assertEqual(corrections.open_entries(), [])

    def test_unfilled_entry_is_open(self):
        for extra in ({}, {"value": ""}):
            entry = Correction(field="header.gross_weight", issue="B/L and EMF disagree", **extra)
            self.assertTrue(entry.is_open)

    def test_value_and_accept_together_rejected(self):
        with self.assertRaises(ValidationError):
            Correction(field="f", issue="i", value=1, accept=True, note="n")

    def test_accept_without_note_rejected(self):
        with self.assertRaises(ValidationError):
            Correction(field="f", issue="i", accept=True)

    def test_zero_is_a_filled_value(self):
        self.assertFalse(Correction(field="f", issue="i", value=0).is_open)

    def test_open_entries_lists_only_unfilled(self):
        data = tomllib.loads(CORRECTIONS_TOML)
        data["correction"].append({"field": "header.eta", "issue": "missing"})
        open_entries = CorrectionsFile.model_validate(data).open_entries()
        self.assertEqual([c.field for c in open_entries], ["header.eta"])


class FlowStateTests(unittest.TestCase):
    def test_builds_empty(self):
        state = ShipmentState()
        self.assertIsNone(state.shipment)
        self.assertEqual(state.corrections, [])


if __name__ == "__main__":
    unittest.main()
